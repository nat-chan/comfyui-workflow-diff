"""
Topological layered layout for ComfyUI workflows.

Given a set of UI-format nodes and link tuples, assign each node an (x, y)
position such that:
    - depth from a root (= longest path length from a node with no incoming
      links) determines the column (x);
    - nodes within the same column stack vertically (y) in deterministic order.

Used by the diff renderer to lay out workflows when the editor-saved
positions are sparse / inconvenient (e.g. one node is at x=-2300 leaving a
huge empty band), and by the API-format adapter where positions don't exist.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

# Default geometry — chosen to give comfortable spacing without being wasteful.
COL_GAP = 80.0
ROW_GAP = 40.0
# Minimum node footprint used for packing when the actual size is missing
# or unreasonable (some workflows leave size=[0, 0] on freshly added nodes).
MIN_W = 220.0
MIN_H = 80.0


def _safe_size(node: dict[str, Any]) -> tuple[float, float]:
    """Return the size to use when packing this node into the layout.

    Layout always uses the clamped (natural) size — packing the raw size
    of an image-preview node would push every other node off the canvas.
    """
    try:
        from .workflow_renderer import clamp_to_natural, TITLE_HEIGHT
    except ImportError:  # standalone / tests
        from workflow_renderer import clamp_to_natural, TITLE_HEIGHT  # type: ignore
    w, h = clamp_to_natural(node)
    w = max(w, MIN_W)
    h = max(h, MIN_H)
    return w, h + TITLE_HEIGHT


def _normalize_node_id(nid: Any) -> str:
    return str(nid)


def _build_graph(
    nodes: list[dict[str, Any]],
    links: list[list[Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]], dict[str, set[str]]]:
    node_by_id: dict[str, dict[str, Any]] = {}
    for n in nodes:
        nid = n.get("id")
        if nid is None:
            continue
        node_by_id[_normalize_node_id(nid)] = n
    upstream: dict[str, set[str]] = defaultdict(set)
    downstream: dict[str, set[str]] = defaultdict(set)
    for link in links:
        if not isinstance(link, (list, tuple)) or len(link) < 5:
            continue
        src = _normalize_node_id(link[1])
        dst = _normalize_node_id(link[3])
        if src in node_by_id and dst in node_by_id:
            upstream[dst].add(src)
            downstream[src].add(dst)
    return node_by_id, upstream, downstream


def _longest_path_depths(
    node_ids: list[str],
    upstream: dict[str, set[str]],
) -> dict[str, int]:
    depth: dict[str, int] = {}
    visiting: set[str] = set()

    def visit(n: str) -> int:
        if n in depth:
            return depth[n]
        if n in visiting:
            # Cycle — break it.
            depth[n] = 0
            return 0
        visiting.add(n)
        ups = upstream.get(n) or ()
        if not ups:
            depth[n] = 0
        else:
            depth[n] = 1 + max(visit(u) for u in ups)
        visiting.discard(n)
        return depth[n]

    for nid in node_ids:
        visit(nid)
    return depth


def layered_layout(
    nodes: list[dict[str, Any]],
    links: list[list[Any]],
    *,
    col_gap: float = COL_GAP,
    row_gap: float = ROW_GAP,
    origin: tuple[float, float] = (0.0, 0.0),
) -> dict[str, tuple[float, float]]:
    """Compute positions for the given UI-format nodes + link tuples.

    Returns a mapping ``{str(node_id): (x, y)}``. Coordinates are absolute
    (top-left of the rendered node including title bar) and are offset by
    ``origin``.
    """
    node_by_id, upstream, _ = _build_graph(nodes, links)
    node_ids = list(node_by_id.keys())
    depth = _longest_path_depths(node_ids, upstream)

    by_layer: dict[int, list[str]] = defaultdict(list)
    for nid, d in depth.items():
        by_layer[d].append(nid)

    # Stable sort within layer: by upstream count desc (nodes with more deps
    # tend to be bigger ⇒ go to top), then by id.
    for layer in by_layer.values():
        layer.sort(key=lambda nid: (-len(upstream.get(nid, ())), _id_sort_key(nid)))

    max_layer = max(by_layer) if by_layer else 0

    # Layer widths = max width across nodes in that layer
    layer_widths: list[float] = []
    for li in range(max_layer + 1):
        layer = by_layer.get(li, [])
        if not layer:
            layer_widths.append(0.0)
            continue
        layer_widths.append(max(_safe_size(node_by_id[n])[0] for n in layer))

    layer_x: list[float] = []
    cursor_x = float(origin[0])
    for w in layer_widths:
        layer_x.append(cursor_x)
        cursor_x += w + col_gap

    positions: dict[str, tuple[float, float]] = {}
    for li in range(max_layer + 1):
        cursor_y = float(origin[1])
        for nid in by_layer.get(li, []):
            _, h = _safe_size(node_by_id[nid])
            positions[nid] = (layer_x[li], cursor_y)
            cursor_y += h + row_gap
    return positions


def _id_sort_key(nid: str) -> tuple:
    try:
        return (0, int(nid))
    except (TypeError, ValueError):
        return (1, nid)


def layered_layout_union(
    nodes_a: list[dict[str, Any]],
    links_a: list[list[Any]],
    nodes_b: list[dict[str, Any]],
    links_b: list[list[Any]],
) -> dict[str, tuple[float, float]]:
    """Compute a single layout from the union of two workflows' nodes/links.

    Useful for diff rendering: every node from either side gets a position,
    and common nodes appear at the same coordinate in both halves. When a
    node exists in both A and B with different sizes, B's size is used (B is
    the "after" view).
    """
    seen: dict[str, dict[str, Any]] = {}
    for n in nodes_a:
        nid = n.get("id")
        if nid is not None:
            seen[_normalize_node_id(nid)] = n
    # B overrides A for size/identity (the "after" state wins).
    for n in nodes_b:
        nid = n.get("id")
        if nid is not None:
            seen[_normalize_node_id(nid)] = n

    # Build a deduplicated link list keyed by endpoint tuple — same identity
    # rule the diff itself uses.
    link_by_sig: dict[tuple, list[Any]] = {}
    for link in (links_a or []) + (links_b or []):
        if not isinstance(link, (list, tuple)) or len(link) < 5:
            continue
        sig = (
            _normalize_node_id(link[1]),
            int(link[2] or 0),
            _normalize_node_id(link[3]),
            int(link[4] or 0),
        )
        link_by_sig.setdefault(sig, list(link))

    return layered_layout(list(seen.values()), list(link_by_sig.values()))
