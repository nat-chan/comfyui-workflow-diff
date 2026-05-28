"""
Resolve widget names for a UI-format workflow node.

ComfyUI's UI workflow JSON stores `widgets_values` as a positional array
*without* the widget names — those live only on the node class via
`INPUT_TYPES()`. This module reads the live ComfyUI node registry and
returns the ordered widget-name list for a given node type, mirroring the
order ComfyUI itself uses to serialize widget values.

When the registry is unavailable (e.g. during unit tests outside ComfyUI),
all functions return empty results and callers should treat names as
unknown.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Try to access the live ComfyUI node registry.  Importing fails outside
# ComfyUI; that's expected and handled.
try:
    import nodes as _comfy_nodes  # type: ignore
except ImportError:  # pragma: no cover
    _comfy_nodes = None


_CONTROL_VALUES = ("fixed", "increment", "decrement", "randomize")


def _has_widget_input_type(input_type: Any) -> bool:
    """Mirror of the logic used in workflow_converter._get_widget_mappings."""
    if isinstance(input_type, (list, tuple)):
        # Combo box (list/tuple of choices)
        return True
    if input_type in ("INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"):
        return True
    if isinstance(input_type, str) and input_type.startswith("COMFY_") and "COMBO" in input_type:
        return True
    if isinstance(input_type, str) and not input_type.isupper():
        # Custom widget types are lowercase
        return True
    return False


def widget_names_for_type(node_type: str) -> list[str]:
    """Return the canonical ordered list of widget names for a node class.

    Returns [] if the node class can't be found or has no widgets.
    """
    if _comfy_nodes is None or not node_type:
        return []
    if not hasattr(_comfy_nodes, "NODE_CLASS_MAPPINGS"):
        return []
    node_class = _comfy_nodes.NODE_CLASS_MAPPINGS.get(node_type)
    if node_class is None:
        return []
    try:
        input_types = node_class.INPUT_TYPES()
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("INPUT_TYPES() failed for %s: %s", node_type, e)
        return []

    widget_names: list[str] = []
    for section in ("required", "optional"):
        if section not in input_types:
            continue
        for input_name, input_spec in input_types[section].items():
            if not isinstance(input_spec, (list, tuple)) or len(input_spec) < 1:
                continue
            if _has_widget_input_type(input_spec[0]):
                widget_names.append(input_name)
    return widget_names


def promoted_input_names(node: dict[str, Any]) -> set[str]:
    """Return the set of widget names that have been promoted to inputs
    on this node via ComfyUI's "Convert widget to input".

    A promoted widget appears in ``node.inputs`` with a ``widget`` meta
    field whose ``name`` is the widget's name. Its slot in
    ``widgets_values`` is preserved (usually as a default placeholder
    like ``""``) but the live value comes from a link, so treating the
    placeholder as a value-diff produces phantom changes.
    """
    out: set[str] = set()
    for inp in node.get("inputs") or []:
        if not isinstance(inp, dict):
            continue
        meta = inp.get("widget")
        if isinstance(meta, dict):
            name = meta.get("name")
            if isinstance(name, str):
                out.add(name)
    return out


def widget_names_for_node(node: dict[str, Any]) -> list[str]:
    """Best-effort widget-name resolution for a single UI-format node.

    The returned list is **always reconciled to the node's
    ``widgets_values`` length** — so callers can iterate the two in
    parallel without bounds-checking. Positions that don't correspond
    to a real user-visible widget get a placeholder name:

    * ``""`` — structural noise (``image_upload`` sentinel, rgthree
      button placeholders, or a widget that's been promoted to an
      input slot — its value comes from a link, not the array)
    * ``"<name>_control"`` — ``control_after_generate`` splice for
      ``<name>``

    Both placeholders are filtered out of the diff and the render so
    the user only sees real user-facing widgets.

    Sources, in priority order:
      1. ``_widget_names`` on the node (set by the API adapter or
         cross-type backfill in the renderer).
      2. The live ComfyUI registry — works inside ComfyUI.
    """
    cached = node.get("_widget_names")
    if isinstance(cached, list) and cached:
        names: list[str] = list(cached)
    else:
        node_type = node.get("type")
        properties = node.get("properties") or {}
        if isinstance(properties, dict) and "Node name for S&R" in properties:
            node_type = properties["Node name for S&R"]
        names = widget_names_for_type(node_type) if isinstance(node_type, str) else []

    if not names:
        return []

    aligned = _reconcile_with_values(names, node.get("widgets_values"))

    # Demote promoted-to-input widget names to "" so the diff and the
    # renderer treat them as structural noise.
    promoted = promoted_input_names(node)
    if promoted:
        aligned = ["" if n in promoted else n for n in aligned]

    return aligned


def is_structural_name(name: str | None) -> bool:
    """Return True for placeholder names that shouldn't be diffed or rendered.

    Only the explicit ``""`` (structural noise / promoted-to-input) and
    ``"<x>_control"`` (control_after_generate splice) placeholders count
    as structural. ``None`` means "we don't know" — those positions
    fall through to positional comparison rather than being silently
    dropped.
    """
    if name is None:
        return False
    if name == "":
        return True
    return name.endswith("_control")


def _reconcile_with_values(names: list[str], widgets_values: Any) -> list[str]:
    """Align widget names with the actual `widgets_values` array.

    ComfyUI inserts a `control_after_generate` shadow value after each INT
    widget that has `control_after_generate` enabled (seed widgets, etc.),
    which makes the serialized array longer than the canonical name list.
    We walk both and insert a synthetic `<name>_control_after_generate`
    name where it lines up.
    """
    if not isinstance(widgets_values, list):
        return names
    aligned: list[str] = []
    name_idx = 0
    val_idx = 0
    while val_idx < len(widgets_values) and name_idx < len(names):
        aligned.append(names[name_idx])
        val = widgets_values[val_idx]
        # If the next slot is a control value, account for it.
        next_val = widgets_values[val_idx + 1] if val_idx + 1 < len(widgets_values) else None
        val_idx += 1
        if next_val in _CONTROL_VALUES:
            aligned.append(f"{names[name_idx]}_control")
            val_idx += 1
        name_idx += 1
    # Pad with positional placeholders if the value list is longer than what we mapped.
    while len(aligned) < len(widgets_values):
        aligned.append("")
    return aligned[: len(widgets_values)]
