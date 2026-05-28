"""
Adapter: synthesize a UI-format-like workflow dict (with `nodes`, `links`,
node positions/sizes and slot lists) from an API-format prompt JSON
(`workflow_api.json`).

This lets the diff renderer — which is built for the UI format — work on
plain API-format prompts too. Positions are computed by the shared
topological layered layout in `layout.py`; the result is readable but it
is *not* a faithful reproduction of how the workflow was authored in the
ComfyUI editor. If the matching UI workflow is available, use that
instead.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

try:
    from .layout import layered_layout
    from .workflow_renderer import _natural_size, TITLE_HEIGHT
except ImportError:  # standalone execution / tests
    from layout import layered_layout  # type: ignore
    from workflow_renderer import _natural_size, TITLE_HEIGHT  # type: ignore


def _is_link_value(value: Any) -> bool:
    """API-format link: ``[source_node_id, source_slot]`` — a two-element
    list/tuple whose first item is a node id and whose second is an int slot.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return False
    sid, slot = value
    if not isinstance(sid, (str, int)):
        return False
    if not isinstance(slot, int):
        return False
    return True


def _coerce_id(nid: Any) -> Any:
    if isinstance(nid, str) and nid.isdigit():
        return int(nid)
    return nid


def _api_to_ui_nodes_and_links(
    prompt: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[list[Any]]]:
    """Pure structural conversion: build UI-format `nodes` + `links` from an
    API-format prompt, *without* assigning positions. Sizes are set to the
    natural size derived from slot+widget counts, which the renderer will
    accept as-is (no further clamping needed).
    """
    if not isinstance(prompt, dict):
        raise TypeError("API prompt must be a dict")

    # Filter to actual nodes (ComfyUI sometimes stashes prompt/extra_data alongside).
    nodes_in: dict[str, dict[str, Any]] = {
        str(k): v
        for k, v in prompt.items()
        if isinstance(v, dict) and "class_type" in v
    }

    per_node_inputs: dict[str, list[tuple[str, Any]]] = {
        nid: list((node.get("inputs") or {}).items()) for nid, node in nodes_in.items()
    }

    out_slots: dict[str, set[int]] = defaultdict(set)
    links: list[list[Any]] = []
    next_link_id = 1
    for nid, items in per_node_inputs.items():
        slot_idx = 0
        for _, value in items:
            if _is_link_value(value):
                src_id = str(value[0])
                src_slot = int(value[1])
                if src_id in nodes_in:
                    out_slots[src_id].add(src_slot)
                    links.append([next_link_id, _coerce_id(src_id), src_slot, _coerce_id(nid), slot_idx, None])
                    next_link_id += 1
                slot_idx += 1

    ui_nodes: list[dict[str, Any]] = []
    for nid, node in nodes_in.items():
        inputs_items = per_node_inputs[nid]
        link_inputs = [(n, v) for n, v in inputs_items if _is_link_value(v)]
        widget_inputs = [(n, v) for n, v in inputs_items if not _is_link_value(v)]

        in_slots = [
            {"name": n, "type": "*", "link": 1}  # link=1 ⇒ renderer fills the dot
            for n, _ in link_inputs
        ]
        max_out_slot = max(out_slots[nid]) if out_slots[nid] else -1
        out_list = [
            {"name": f"out{i}", "type": "*", "links": [1] if i in out_slots[nid] else []}
            for i in range(max_out_slot + 1)
        ]

        widgets_values = [v for _, v in widget_inputs]
        widget_names = [n for n, _ in widget_inputs]

        # Use the natural size directly — there's no editor-supplied size to clamp.
        ui_node: dict[str, Any] = {
            "id": _coerce_id(nid),
            "type": node.get("class_type"),
            "title": (node.get("_meta") or {}).get("title") or node.get("class_type"),
            "pos": [0.0, 0.0],
            "inputs": in_slots,
            "outputs": out_list,
            "widgets_values": widgets_values,
            "_widget_names": widget_names,
        }
        nat_w, nat_h = _natural_size(ui_node)
        ui_node["size"] = [nat_w, nat_h]
        ui_nodes.append(ui_node)

    return ui_nodes, links


def api_to_ui_workflow(
    prompt: dict[str, Any],
    *,
    layout_hints: dict[str, tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Convert an API-format prompt to a UI-format workflow with positions.

    Positions are computed by ``layout.layered_layout``. ``layout_hints``
    (id → (x, y)) overrides individual positions; when both sides of a
    diff share the same hints, the rendered diff lines up.
    """
    nodes, links = _api_to_ui_nodes_and_links(prompt)
    positions = layered_layout(nodes, links)

    for n in nodes:
        nid = str(n["id"])
        if layout_hints and nid in layout_hints:
            x, y = layout_hints[nid]
        elif nid in positions:
            x, y = positions[nid]
        else:
            x, y = 0.0, 0.0
        n["pos"] = [x, y]

    return {"nodes": nodes, "links": links}


