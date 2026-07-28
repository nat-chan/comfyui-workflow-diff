"""
Server-side re-implementation of the ComfyUI frontend's
"Load (API format) → Save" pipeline.

Mirrors comfyui_frontend_package 1.45.19 (the version bundled in this
venv — verified against the minified sources in
``comfyui_frontend_package/static/assets``):

* ``ComfyApp.loadApiJson`` — create one litegraph node per API entry
  (id, title from ``_meta.title``), then run the connection pass TWICE
  (the frontend literally does; the second pass disconnects and
  re-creates every link, so final link ids occupy ``N+1..2N``), calling
  ``graph.arrange()`` after each pass.
* Node construction (``useLitegraphService.addNodeInput``) — every input
  spec (required then optional, hidden excluded) yields exactly one
  input slot; widget-typed inputs (COMBO list / INT / FLOAT / STRING /
  BOOLEAN / "COMBO", unless ``forceInput``) additionally create a widget
  and their slot carries ``widget: {name}``. INT inputs named
  ``seed``/``noise_seed`` or flagged ``control_after_generate`` get an
  extra ``control_after_generate`` combo widget (default "randomize";
  FLOAT flavour defaults to "fixed") which IS serialized into
  ``widgets_values``.
* ``LGraph.arrange(100)`` / ``computeExecutionOrder`` — layered layout:
  column per link-depth level, 100px margins, columns start at
  y = margin + NODE_TITLE_HEIGHT.
* ``LGraphNode.serialize`` / ``inputAsSerialisable`` /
  ``outputAsSerialisable`` and the save-path post-processing
  (``compressWidgetInputSlots``): unconnected widget slots are dropped
  from ``inputs`` and link ``target_slot`` is rewritten to the
  compressed index; ``extra.frontendVersion`` is stamped.

Known deviations from a real browser (documented, unavoidable headless):

* Node sizes use litegraph's *fallback* text measure
  (``NODE_TEXT_SIZE * len * 0.6``) because there is no canvas
  ``measureText`` server-side — positions/sizes are therefore close to,
  but not bit-identical with, a real browser save.
* Frontend-extension widgets (e.g. the LoadImage upload button) are not
  replicated — only spec-driven widgets appear in ``widgets_values``.
* ``properties.cnr_id``/``ver`` are stamped for comfy-core nodes only;
  registry ids of third-party packs are not known server-side.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# litegraph constants (frontend 1.45.19)
_NODE_TEXT_SIZE = 14
_NODE_TITLE_HEIGHT = 30
_NODE_SLOT_HEIGHT = 20
_NODE_WIDTH = 140
_NODE_WIDGET_HEIGHT = 20
# BaseWidget: minValueWidth=42, margin=15, arrowMargin=6, arrowWidth=10
_WIDGET_VALUE_EXTRA = 42 + 2 * (15 + 6 + 10)
_SHAPE_HOLLOW_CIRCLE = 7  # optional input slots
_SHAPE_GRID = 6  # OUTPUT_IS_LIST outputs
_ARRANGE_MARGIN = 100
_MULTILINE_WIDGET_HEIGHT = 80  # approximation of the textarea widget

_WIDGET_TYPE_NAMES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}


def _measure_text(text: str) -> float:
    """litegraph's headless fallback: NODE_TEXT_SIZE * length * 0.6."""
    return _NODE_TEXT_SIZE * len(text or "") * 0.6


class _Slot:
    """An input slot as litegraph models it (socket or widget-backed)."""

    __slots__ = ("name", "type", "shape", "widget_name", "link")

    def __init__(
        self,
        name: str,
        type_: str,
        *,
        shape: int | None = None,
        widget_name: str | None = None,
    ) -> None:
        self.name = name
        self.type = type_
        self.shape = shape
        self.widget_name = widget_name  # set => widget input slot
        self.link: int | None = None


class _Output:
    __slots__ = ("name", "type", "shape", "links")

    def __init__(self, name: str, type_: str, *, shape: int | None = None) -> None:
        self.name = name
        self.type = type_
        self.shape = shape
        self.links: list[int] | None = None  # null until first connection


class _Widget:
    __slots__ = ("name", "value", "multiline")

    def __init__(self, name: str, value: Any, *, multiline: bool = False) -> None:
        self.name = name
        self.value = value
        self.multiline = multiline


class _Node:
    __slots__ = (
        "id",
        "type",
        "title",
        "default_title",
        "inputs",
        "outputs",
        "widgets",
        "pos",
        "size",
        "order",
        "level",
        "properties",
    )

    def __init__(self, node_id: int | str, class_type: str) -> None:
        self.id = node_id
        self.type = class_type
        self.title: str = class_type
        self.default_title: str = class_type
        self.inputs: list[_Slot] = []
        self.outputs: list[_Output] = []
        self.widgets: list[_Widget] = []
        self.pos: list[float] = [0.0, 0.0]
        self.size: list[float] = [0.0, 0.0]
        self.order = 0
        self.level = 0
        self.properties: dict[str, Any] = {}


class _Link:
    __slots__ = ("id", "type", "origin_id", "origin_slot", "target_id", "target_slot")

    def __init__(
        self,
        link_id: int,
        type_: str,
        origin_id: int | str,
        origin_slot: int,
        target_id: int | str,
        target_slot: int,
    ) -> None:
        self.id = link_id
        self.type = type_
        self.origin_id = origin_id
        self.origin_slot = origin_slot
        self.target_id = target_id
        self.target_slot = target_slot


# --------------------------------------------------------------- node defs
def _iter_input_specs(input_types: dict[str, Any]) -> list[tuple[str, Any, dict[str, Any], bool]]:
    """Yield ``(name, type, options, is_optional)`` — required then optional,
    hidden excluded — matching the frontend's getOrderedInputSpecs."""
    out: list[tuple[str, Any, dict[str, Any], bool]] = []
    for section, optional in (("required", False), ("optional", True)):
        for name, spec in (input_types.get(section) or {}).items():
            if isinstance(spec, (list, tuple)) and spec:
                type_ = spec[0]
                options = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            else:
                type_, options = spec, {}
            out.append((name, type_, options, optional))
    return out


def _is_widget_input(type_: Any, options: dict[str, Any]) -> bool:
    """Mirror of the frontend widget registry lookup + forceInput escape."""
    if options.get("forceInput"):
        return False
    if isinstance(type_, (list, tuple)):
        return True  # combo choices
    return isinstance(type_, str) and type_ in _WIDGET_TYPE_NAMES


def _slot_type_name(type_: Any) -> str:
    """Slot 'type' string as the V1→V2 spec transform produces it."""
    if isinstance(type_, (list, tuple)):
        return "COMBO"
    return str(type_)


def _widget_default(type_: Any, options: dict[str, Any]) -> Any:
    if "default" in options:
        return options["default"]
    if isinstance(type_, (list, tuple)):
        return type_[0] if len(type_) else None
    if type_ == "COMBO":
        combo_options = options.get("options")
        if isinstance(combo_options, (list, tuple)) and combo_options:
            return combo_options[0]
        return None
    if type_ == "INT":
        return 0
    if type_ == "FLOAT":
        return 0.0
    if type_ == "STRING":
        return ""
    if type_ == "BOOLEAN":
        return False
    return None


def _control_after_generate_value(name: str, type_: Any, options: dict[str, Any]) -> str | None:
    """Return the control widget's initial value, or None if no control
    widget is attached to this input. Mirrors useIntWidget /
    useFloatWidget / addComboWidget in the frontend."""
    cag = options.get("control_after_generate")
    if isinstance(type_, (list, tuple)) or type_ == "COMBO":
        if not cag:
            return None
        return cag if isinstance(cag, str) else "randomize"
    if type_ == "INT":
        if not (cag or name in ("seed", "noise_seed")):
            return None
        return cag if isinstance(cag, str) else "randomize"
    if type_ == "FLOAT":
        if not cag:
            return None
        return cag if isinstance(cag, str) else "fixed"
    return None


def _comfy_core_version() -> str | None:
    try:
        from comfyui_version import __version__  # available inside ComfyUI

        return __version__
    except Exception:
        return None


def _frontend_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("comfyui_frontend_package")
    except Exception:
        return None


def _build_node(
    node_id: int | str,
    api_node: dict[str, Any],
    node_class: type,
    display_name: str,
    comfy_ver: str | None,
) -> _Node | None:
    class_type = str(api_node.get("class_type"))
    try:
        input_types = node_class.INPUT_TYPES()
    except Exception:
        logger.exception("INPUT_TYPES() failed for %s", class_type)
        return None

    node = _Node(node_id, class_type)
    node.default_title = display_name
    meta = api_node.get("_meta")
    title = meta.get("title") if isinstance(meta, dict) else None
    node.title = title if isinstance(title, str) else display_name

    for name, type_, options, optional in _iter_input_specs(input_types):
        shape = _SHAPE_HOLLOW_CIRCLE if optional else None
        if _is_widget_input(type_, options):
            node.widgets.append(
                _Widget(
                    name,
                    _widget_default(type_, options),
                    multiline=bool(options.get("multiline")),
                )
            )
            control = _control_after_generate_value(name, type_, options)
            if control is not None:
                node.widgets.append(_Widget("control_after_generate", control))
                if isinstance(type_, (list, tuple)) or type_ == "COMBO":
                    # addValueControlWidgets adds a filter widget for combos
                    node.widgets.append(_Widget("control_filter_list", ""))
            node.inputs.append(_Slot(name, _slot_type_name(type_), shape=shape, widget_name=name))
        else:
            node.inputs.append(_Slot(name, _slot_type_name(type_), shape=shape))

    return_types = getattr(node_class, "RETURN_TYPES", ()) or ()
    return_names = getattr(node_class, "RETURN_NAMES", None) or ()
    output_is_list = getattr(node_class, "OUTPUT_IS_LIST", None) or ()
    for i, rtype in enumerate(return_types):
        type_name = _slot_type_name(rtype)
        name = return_names[i] if i < len(return_names) else type_name
        is_list = bool(output_is_list[i]) if i < len(output_is_list) else False
        node.outputs.append(_Output(str(name), type_name, shape=_SHAPE_GRID if is_list else None))

    node.properties["Node name for S&R"] = class_type
    module_top = getattr(node_class, "__module__", "").split(".")[0]
    if module_top in ("nodes", "comfy_extras", "comfy_api_nodes"):
        node.properties["cnr_id"] = "comfy-core"
        if comfy_ver:
            node.properties["ver"] = comfy_ver

    return node


# ------------------------------------------------------------- connections
def _is_link_value(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and not isinstance(value[0], bool)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


def _is_valid_connection(out_type: str, in_type: str) -> bool:
    """litegraph.isValidConnection — '*'/empty match everything, otherwise
    the comma-separated type lists must intersect."""
    if not out_type or not in_type or out_type == "*" or in_type == "*":
        return True
    return bool(set(out_type.split(",")) & set(in_type.split(",")))


def _common_type(in_type: str, out_type: str) -> str:
    """link.type = commonType(input, output) || input.type || output.type"""
    if in_type and out_type:
        common = [t for t in in_type.split(",") if t in set(out_type.split(","))]
        if common:
            return common[0]
        if out_type == "*":
            return in_type
        if in_type == "*":
            return out_type
    return in_type or out_type


class _Graph:
    def __init__(self) -> None:
        self.nodes: list[_Node] = []
        self.by_id: dict[str, _Node] = {}
        self.links: dict[int, _Link] = {}
        self.last_link_id = 0

    def add(self, node: _Node) -> None:
        self.nodes.append(node)
        self.by_id[str(node.id)] = node

    def get(self, node_id: Any) -> _Node | None:
        return self.by_id.get(str(node_id))

    def connect(self, origin: _Node, origin_slot: int, target: _Node, target_slot: int) -> None:
        """LGraphNode.connectSlots — replaces any existing link on the
        target input and allocates a fresh link id."""
        if origin_slot >= len(origin.outputs) or target_slot >= len(target.inputs):
            return
        out = origin.outputs[origin_slot]
        slot = target.inputs[target_slot]
        if not _is_valid_connection(out.type, slot.type):
            return
        if slot.link is not None:
            old = self.links.pop(slot.link, None)
            if old is not None:
                old_origin = self.get(old.origin_id)
                if old_origin is not None and old.origin_slot < len(old_origin.outputs):
                    links = old_origin.outputs[old.origin_slot].links
                    if links and old.id in links:
                        links.remove(old.id)
        self.last_link_id += 1
        link = _Link(
            self.last_link_id,
            _common_type(slot.type, out.type),
            origin.id,
            origin_slot,
            target.id,
            target_slot,
        )
        self.links[link.id] = link
        if out.links is None:
            out.links = []
        out.links.append(link.id)
        slot.link = link.id

    # -------------------------------------------------- layout (arrange)
    def compute_execution_order(self) -> list[_Node]:
        """LGraph.computeExecutionOrder(false, true) — Kahn's algorithm
        with per-link level propagation; leftovers (cycles) appended."""
        remaining: dict[str, _Node] = {}
        ready: list[_Node] = []
        pending_inputs: dict[str, int] = {}
        visited_links: set[int] = set()

        for node in self.nodes:
            remaining[str(node.id)] = node
            count = sum(1 for slot in node.inputs if slot.link is not None)
            if count == 0:
                ready.append(node)
                node.level = 1
            else:
                node.level = 0
                pending_inputs[str(node.id)] = count

        ordered: list[_Node] = []
        while ready:
            node = ready.pop(0)
            ordered.append(node)
            remaining.pop(str(node.id), None)
            for out in node.outputs:
                for link_id in out.links or []:
                    link = self.links.get(link_id)
                    if link is None or link.id in visited_links:
                        continue
                    target = self.get(link.target_id)
                    if target is None:
                        visited_links.add(link.id)
                        continue
                    if not target.level or target.level <= node.level:
                        target.level = node.level + 1
                    visited_links.add(link.id)
                    pending_inputs[str(target.id)] -= 1
                    if pending_inputs[str(target.id)] == 0:
                        ready.append(target)
        ordered.extend(remaining.values())
        for i, node in enumerate(ordered):
            node.order = i
        return ordered

    def arrange(self) -> None:
        """LGraph.arrange(100) — one column per level, horizontal layout."""
        ordered = self.compute_execution_order()
        columns: dict[int, list[_Node]] = {}
        for node in ordered:
            columns.setdefault(node.level or 1, []).append(node)
        margin = float(_ARRANGE_MARGIN)
        x = margin
        for level in sorted(columns):
            column = columns[level]
            max_width = 100.0
            y = margin + _NODE_TITLE_HEIGHT
            for node in column:
                node.pos = [x, y]
                max_width = max(max_width, node.size[0])
                y += node.size[1] + margin + _NODE_TITLE_HEIGHT
            x += max_width + margin


# ------------------------------------------------------------ compute size
def _compute_size(node: _Node) -> list[float]:
    """LGraphNode.computeSize with the headless text-measure fallback."""
    rows = max(
        sum(1 for s in node.inputs if s.widget_name is None),
        len(node.outputs),
        1,
    )
    title_width = _NODE_TITLE_HEIGHT + _measure_text(node.title) + _NODE_TITLE_HEIGHT * 0.33

    input_width = 0.0
    widget_label_width = 0.0
    for slot in node.inputs:
        w = _measure_text(slot.name)
        if slot.widget_name is not None:
            widget_label_width = max(widget_label_width, w)
        else:
            input_width = max(input_width, w)
    output_width = max((_measure_text(o.name) for o in node.outputs), default=0.0)

    base_width = _NODE_WIDTH * (1.5 if node.widgets else 1)
    gap = 5 if input_width and output_width else 0
    slots_width = input_width + output_width + 2 * _NODE_SLOT_HEIGHT + gap
    if widget_label_width:
        widget_label_width += _WIDGET_VALUE_EXTRA

    width = max(slots_width, widget_label_width, title_width, base_width)
    height = float(rows * _NODE_SLOT_HEIGHT)
    widgets_height = 0.0
    for widget in node.widgets:
        row = _MULTILINE_WIDGET_HEIGHT if widget.multiline else _NODE_WIDGET_HEIGHT
        widgets_height += row + 4
    if node.widgets:
        widgets_height += 8
    height += widgets_height + 6
    return [width, height]


# --------------------------------------------------------------- serialize
def _serialize_node(node: _Node) -> dict[str, Any]:
    """LGraphNode.serialize + save-path compression (unconnected widget
    slots dropped; ``localized_name`` never emitted)."""
    inputs: list[dict[str, Any]] = []
    for slot in node.inputs:
        if slot.widget_name is not None and slot.link is None:
            continue  # compressWidgetInputSlots/matchesLegacyApi
        entry: dict[str, Any] = {"name": slot.name}
        if slot.shape is not None:
            entry["shape"] = slot.shape
        entry["type"] = slot.type
        if slot.widget_name is not None:
            entry["widget"] = {"name": slot.widget_name}
        entry["link"] = slot.link
        inputs.append(entry)

    outputs: list[dict[str, Any]] = []
    for out in node.outputs:
        entry = {"name": out.name}
        if out.shape is not None:
            entry["shape"] = out.shape
        entry["type"] = out.type
        entry["links"] = out.links
        outputs.append(entry)

    data: dict[str, Any] = {
        "id": node.id,
        "type": node.type,
        "pos": [node.pos[0], node.pos[1]],
        "size": [node.size[0], node.size[1]],
        "flags": {},
        "order": node.order,
        "mode": 0,
        "inputs": inputs,
        "outputs": outputs,
    }
    if node.title != node.default_title:
        data["title"] = node.title
    data["properties"] = dict(node.properties)
    data["widgets_values"] = [w.value for w in node.widgets]
    return data


def _compressed_target_slot(node: _Node, target_slot: int) -> int:
    """Index of ``target_slot`` after unconnected widget slots are removed."""
    compressed = 0
    for i, slot in enumerate(node.inputs):
        if slot.widget_name is not None and slot.link is None:
            continue
        if i == target_slot:
            return compressed
        compressed += 1
    return target_slot


# ------------------------------------------------------------- entry point
def convert_api_to_ui(
    prompt: dict[str, Any],
    *,
    node_class_mappings: dict[str, type] | None = None,
    node_display_name_mappings: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Convert an API-format prompt into the UI workflow JSON the
    frontend would save after loading it. Returns ``(workflow,
    missing_class_types)``; nodes whose class is not registered are
    skipped exactly like the frontend does (it shows a toast and
    proceeds).
    """
    if node_class_mappings is None or node_display_name_mappings is None:
        import nodes  # ComfyUI runtime — raises ImportError outside ComfyUI

        node_class_mappings = node_class_mappings or nodes.NODE_CLASS_MAPPINGS
        node_display_name_mappings = node_display_name_mappings or nodes.NODE_DISPLAY_NAME_MAPPINGS

    api_nodes: dict[str, dict[str, Any]] = {
        str(k): v for k, v in prompt.items() if isinstance(v, dict) and "class_type" in v
    }

    missing = sorted(
        {
            str(n["class_type"])
            for n in api_nodes.values()
            if str(n["class_type"]) not in node_class_mappings
        }
    )

    comfy_ver = _comfy_core_version()
    graph = _Graph()
    for raw_id, api_node in api_nodes.items():
        class_type = str(api_node["class_type"])
        node_class = node_class_mappings.get(class_type)
        if node_class is None:
            continue  # frontend: createNode returns null → node skipped
        node_id: int | str = int(raw_id) if raw_id.lstrip("-").isdigit() else raw_id
        display_name = node_display_name_mappings.get(class_type, class_type)
        node = _build_node(node_id, api_node, node_class, display_name, comfy_ver)
        if node is not None:
            graph.add(node)

    def process_node_inputs(raw_id: str) -> None:
        node = graph.get(raw_id)
        if node is None:
            return
        inputs = api_nodes[raw_id].get("inputs")
        if not isinstance(inputs, dict):
            return
        for input_name, value in inputs.items():
            if _is_link_value(value):
                origin = graph.get(value[0])
                if origin is None:
                    continue
                to_slot = next((i for i, s in enumerate(node.inputs) if s.name == input_name), -1)
                if to_slot != -1:
                    graph.connect(origin, int(value[1]), node, to_slot)
            elif value is not None:
                for widget in node.widgets:
                    if widget.name == input_name:
                        widget.value = value
                        break

    # sizes are needed before arrange() reads them
    for node in graph.nodes:
        node.size = _compute_size(node)

    # the frontend runs the connect+arrange cycle twice; the second pass
    # re-creates every link, shifting ids to N+1..2N
    for _pass in range(2):
        for raw_id in api_nodes:
            process_node_inputs(raw_id)
        graph.arrange()

    links_serialized = [
        [
            link.id,
            link.origin_id,
            link.origin_slot,
            link.target_id,
            _compressed_target_slot(target, link.target_slot)
            if (target := graph.get(link.target_id)) is not None
            else link.target_slot,
            link.type,
        ]
        for link in graph.links.values()
    ]

    int_ids = [n.id for n in graph.nodes if isinstance(n.id, int)]
    workflow: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "revision": 0,
        "last_node_id": max(int_ids, default=0),
        "last_link_id": graph.last_link_id,
        "nodes": [_serialize_node(n) for n in graph.nodes],
        "links": links_serialized,
        "groups": [],
        "config": {},
        "extra": {},
        "version": 0.4,
    }
    fe_ver = _frontend_version()
    if fe_ver:
        workflow["extra"]["frontendVersion"] = fe_ver
    return workflow, missing
