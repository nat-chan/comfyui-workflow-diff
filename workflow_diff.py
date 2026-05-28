"""
Compute a structural diff between two ComfyUI workflow JSON documents
(the non-API format, i.e. what 'Save' produces — with `nodes`, `links`,
positions and sizes).

The result is consumed by `workflow_renderer.py` to draw a litegraph-style
SVG annotated with red (removed), green (added), and yellow (changed)
highlights.
"""

from __future__ import annotations

from typing import Any

try:
    from .widget_names import is_structural_name, widget_names_for_node
except ImportError:  # standalone execution / tests
    from widget_names import is_structural_name, widget_names_for_node  # type: ignore


def _normalize_size(node: dict[str, Any]) -> tuple[float, float]:
    size = node.get("size", [200, 100])
    if isinstance(size, dict):
        w = float(size.get("0", size.get(0, 200)))
        h = float(size.get("1", size.get(1, 100)))
    elif isinstance(size, (list, tuple)) and len(size) >= 2:
        w = float(size[0])
        h = float(size[1])
    else:
        w, h = 200.0, 100.0
    return w, h


def _normalize_pos(node: dict[str, Any]) -> tuple[float, float]:
    pos = node.get("pos", [0, 0])
    if isinstance(pos, dict):
        x = float(pos.get("0", pos.get(0, 0)))
        y = float(pos.get("1", pos.get(1, 0)))
    elif isinstance(pos, (list, tuple)) and len(pos) >= 2:
        x = float(pos[0])
        y = float(pos[1])
    else:
        x, y = 0.0, 0.0
    return x, y


def _index_nodes(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(n.get("id")): n for n in workflow.get("nodes", []) if n.get("id") is not None}


def _link_signature(link: list[Any] | tuple[Any, ...]) -> tuple:
    # link tuple: [id, source_id, source_slot, target_id, target_slot, type]
    # Identify links by their endpoints (ignore the id, which is volatile).
    if not isinstance(link, (list, tuple)) or len(link) < 5:
        return ("__invalid__",)
    return (str(link[1]), int(link[2] or 0), str(link[3]), int(link[4] or 0))


def _index_links(workflow: dict[str, Any]) -> dict[tuple, list[Any]]:
    out: dict[tuple, list[Any]] = {}
    for link in workflow.get("links", []) or []:
        sig = _link_signature(link)
        out[sig] = list(link)
    return out


def _widgets_diff(
    values_a: Any,
    values_b: Any,
    *,
    names_a: list[str] | None = None,
    names_b: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return a list of change records for widgets that differ.

    Each record is ``{"index": i, "name": str|None, "old": old, "new": new}``
    where ``i`` is the row index in the *B* widget list (so the renderer
    can highlight the correct row). When ``old`` or ``new`` is missing
    (widget added/removed), the value is the sentinel string ``"<missing>"``.

    When both ``names_a`` and ``names_b`` are provided, comparison is done
    by widget name (so renaming/reordering doesn't produce noise).
    """
    if values_a is None and values_b is None:
        return []
    if not isinstance(values_a, list):
        values_a = []
    if not isinstance(values_b, list):
        values_b = []

    out: list[dict[str, Any]] = []

    if names_a and names_b:
        a_by_name = dict(zip(names_a, values_a))
        for i, name in enumerate(names_b):
            # Skip structural / control_after_generate placeholders.
            if is_structural_name(name):
                continue
            bv = values_b[i] if i < len(values_b) else _MISSING
            av = a_by_name.get(name, _MISSING)
            if av != bv:
                out.append(
                    {
                        "index": i,
                        "name": name or None,
                        "old": _MISSING_STR if av is _MISSING else av,
                        "new": _MISSING_STR if bv is _MISSING else bv,
                    }
                )
        return out

    n = max(len(values_a), len(values_b))
    for i in range(n):
        # Figure out a name for this position (B side wins, A side is fallback).
        # We keep the empty string as-is here — that's the explicit
        # structural marker; only fall back to None when no side has any
        # info for this position.
        name: str | None = None
        if names_b and i < len(names_b):
            name = names_b[i]
        elif names_a and i < len(names_a):
            name = names_a[i]
        # Skip structural slots whichever side they appeared on.
        if is_structural_name(name):
            continue
        av = values_a[i] if i < len(values_a) else _MISSING
        bv = values_b[i] if i < len(values_b) else _MISSING
        if av != bv:
            out.append(
                {
                    "index": i,
                    "name": name or None,
                    "old": _MISSING_STR if av is _MISSING else av,
                    "new": _MISSING_STR if bv is _MISSING else bv,
                }
            )
    return out


_MISSING = object()
_MISSING_STR = "<missing>"


def _build_common_entry(
    na: dict[str, Any],
    nb: dict[str, Any],
) -> dict[str, Any]:
    """Build a single ``common_nodes`` entry from a pair (A-side, B-side)."""
    names_a = widget_names_for_node(na)
    names_b = widget_names_for_node(nb)
    changed_widgets = _widgets_diff(
        na.get("widgets_values"),
        nb.get("widgets_values"),
        names_a=names_a or None,
        names_b=names_b or None,
    )
    type_changed = na.get("type") != nb.get("type")
    pa, pb = _normalize_pos(na), _normalize_pos(nb)
    sa, sb = _normalize_size(na), _normalize_size(nb)
    moved = pa != pb or sa != sb
    return {
        "a": na,
        "b": nb,
        "changed_widgets": changed_widgets,
        "type_changed": type_changed,
        "moved": moved,
    }


def _unique_type_pairing(
    nodes_a: dict[str, dict[str, Any]],
    nodes_b: dict[str, dict[str, Any]],
    added_ids: set[str],
    removed_ids: set[str],
) -> dict[str, str]:
    """Pair added/removed nodes that share a ``class_type`` 1:1.

    Returns ``{old_a_id: new_b_id}`` for every type where there is
    *exactly* one added and one removed node. Anything ambiguous
    (multiple of the same type on either side) is left alone, so the
    rule never collapses distinct nodes by accident.

    A typical hit: the user deleted a "Seed (rgthree)" node and added a
    fresh one (with the same widget values) — ComfyUI gave the new node
    a different id, but conceptually it's the same node moved/renamed.
    """
    added_by_type: dict[str, list[str]] = {}
    removed_by_type: dict[str, list[str]] = {}
    for nid in added_ids:
        t = nodes_b[nid].get("type")
        if isinstance(t, str):
            added_by_type.setdefault(t, []).append(nid)
    for nid in removed_ids:
        t = nodes_a[nid].get("type")
        if isinstance(t, str):
            removed_by_type.setdefault(t, []).append(nid)

    remap: dict[str, str] = {}
    for t, a_list in added_by_type.items():
        r_list = removed_by_type.get(t, [])
        if len(a_list) == 1 and len(r_list) == 1:
            remap[r_list[0]] = a_list[0]
    return remap


def _remap_link_endpoints(
    link: list[Any],
    id_remap: dict[str, str],
    nodes_b: dict[str, dict[str, Any]],
) -> list[Any]:
    """Return ``link`` with src/dst ids substituted via ``id_remap``,
    preserving the original numeric/str id type so signatures match."""
    if not isinstance(link, (list, tuple)) or len(link) < 5:
        return list(link)
    new_link = list(link)
    src = str(link[1])
    dst = str(link[3])
    if src in id_remap:
        new_link[1] = nodes_b[id_remap[src]].get("id")
    if dst in id_remap:
        new_link[3] = nodes_b[id_remap[dst]].get("id")
    return new_link


def diff_workflows(workflow_a: dict[str, Any], workflow_b: dict[str, Any]) -> dict[str, Any]:
    """Diff two ComfyUI workflow JSON dicts.

    Returns a dict with keys:
        added_nodes:   list[node]  (only in B, after id-pairing rules)
        removed_nodes: list[node]  (only in A, after id-pairing rules)
        common_nodes:  list[{a, b, changed_widgets, type_changed, moved}]
        added_links:   list[link]
        removed_links: list[link]
        common_links:  list[link]

    Identity rules:
        * Nodes are first matched by id. Anything still in
          *added* or *removed* afterwards is then run through a
          unique-type pairing pass: when there is exactly one added and
          one removed node of the same ``class_type``, they are
          treated as the same node (id was renamed) and moved to
          ``common_nodes``. The paired entry is flagged via
          ``id_renamed = (old_a_id, new_b_id)``.
        * Links are matched by their endpoint tuple
          ``(src_id, src_slot, dst_id, dst_slot)``. The A side's link
          endpoints are remapped through the id-pairing first, so a
          link that connects to a renamed node still matches its
          B-side counterpart.
    """
    nodes_a = _index_nodes(workflow_a)
    nodes_b = _index_nodes(workflow_b)

    added_ids = nodes_b.keys() - nodes_a.keys()
    removed_ids = nodes_a.keys() - nodes_b.keys()
    common_ids = nodes_a.keys() & nodes_b.keys()

    # Unique-type pairing for the otherwise-unmatched nodes.
    id_remap = _unique_type_pairing(nodes_a, nodes_b, added_ids, removed_ids)
    paired_a = set(id_remap.keys())
    paired_b = set(id_remap.values())

    # Build common entries: original (same-id) pairs plus the unique-type
    # pairs (different ids).
    common_nodes: list[dict[str, Any]] = []
    for nid in common_ids:
        common_nodes.append(_build_common_entry(nodes_a[nid], nodes_b[nid]))
    for old_a, new_b in id_remap.items():
        entry = _build_common_entry(nodes_a[old_a], nodes_b[new_b])
        entry["id_renamed"] = (old_a, new_b)
        common_nodes.append(entry)

    # Link diff with id_remap applied to A's link endpoints, so a link
    # that touched a renamed node still matches its B-side twin.
    links_a_remapped: dict[tuple, list[Any]] = {}
    for link in workflow_a.get("links") or []:
        remapped = _remap_link_endpoints(link, id_remap, nodes_b)
        links_a_remapped[_link_signature(remapped)] = remapped
    links_b = _index_links(workflow_b)

    added_link_keys = links_b.keys() - links_a_remapped.keys()
    removed_link_keys = links_a_remapped.keys() - links_b.keys()
    common_link_keys = links_a_remapped.keys() & links_b.keys()

    return {
        "added_nodes": [
            nodes_b[i] for i in sorted(added_ids - paired_b, key=_id_sort)
        ],
        "removed_nodes": [
            nodes_a[i] for i in sorted(removed_ids - paired_a, key=_id_sort)
        ],
        "common_nodes": common_nodes,
        "added_links": [links_b[k] for k in added_link_keys],
        "removed_links": [links_a_remapped[k] for k in removed_link_keys],
        "common_links": [links_b[k] for k in common_link_keys],
    }


def _id_sort(s: str):
    try:
        return (0, int(s))
    except (TypeError, ValueError):
        return (1, str(s))
