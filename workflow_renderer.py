"""
Render a ComfyUI (litegraph) workflow as an SVG image.

The renderer is intentionally a single-file, pure-Python module with no
runtime dependencies — it just emits SVG markup. The output mimics the
default ComfyUI / litegraph look (dark background, rounded node bodies,
colored title bar, slot dots, bezier links).

When a `diff` payload (from `workflow_diff.diff_workflows`) is passed, the
output is annotated with:
    - red    for removed nodes / links
    - green  for added nodes / links
    - yellow highlight for changed widget rows / moved nodes
"""

from __future__ import annotations

from html import escape as _esc
from typing import Any, Iterable

try:
    from .workflow_diff import _normalize_pos, _normalize_size
    from .widget_names import is_structural_name, widget_names_for_node
    from .layout import layered_layout_union
except ImportError:  # standalone execution / tests
    from workflow_diff import _normalize_pos, _normalize_size  # type: ignore
    from widget_names import is_structural_name, widget_names_for_node  # type: ignore
    from layout import layered_layout_union  # type: ignore

# ------------------------------------------------------------------ palette
# Tuned to the litegraph defaults that ship with ComfyUI's frontend.
BG_COLOR = "#202020"
GRID_COLOR = "#262626"
NODE_BG = "#353535"
NODE_BG_BYPASSED = "#4a3f5a"
NODE_BG_MUTED = "#3a3a3a"
NODE_BORDER = "#1a1a1a"
NODE_BORDER_SELECTED = "#FFF"
TITLE_BG = "#353535"
TITLE_BG_DEFAULT = "#353535"
TITLE_TEXT = "#FFFFFF"
SLOT_TEXT = "#DDDDDD"
WIDGET_BG = "#2c2c2c"
WIDGET_TEXT = "#E0E0E0"
WIDGET_OUTLINE = "#1e1e1e"

DIFF_ADDED = "#22c55e"          # green
DIFF_REMOVED = "#ef4444"        # red
DIFF_CHANGED = "#fbbf24"        # amber/yellow
DIFF_MOVED = "#60a5fa"          # blue

# Slot colors keyed by ComfyUI socket type. These follow the conventions
# used by the litegraph frontend; unknown types fall back to gray.
SLOT_COLORS: dict[str, str] = {
    "MODEL": "#b39ddb",
    "CONDITIONING": "#FFA931",
    "VAE": "#FF6E6E",
    "LATENT": "#FF9CF9",
    "IMAGE": "#64B5F6",
    "CLIP": "#FFD500",
    "CLIP_VISION": "#A8DADC",
    "CLIP_VISION_OUTPUT": "#AD7452",
    "STYLE_MODEL": "#C2FFAE",
    "MASK": "#81C784",
    "CONTROL_NET": "#A6A9AA",
    "INT": "#29699C",
    "FLOAT": "#88A3BA",
    "STRING": "#F0F0F0",
    "BOOLEAN": "#FF4D4D",
    "AUDIO": "#FFD7B5",
    "GUIDER": "#E0AFFF",
    "SAMPLER": "#FFC0CB",
    "SIGMAS": "#FFC0CB",
    "NOISE": "#B0E0E6",
}
SLOT_DEFAULT = "#9aa0a6"

# Geometry constants (litegraph defaults).
TITLE_HEIGHT = 30
SLOT_HEIGHT = 20
SLOT_RADIUS = 4.5
SLOT_LABEL_PAD = 14
WIDGET_HEIGHT = 20
NODE_RADIUS = 8


def _size_with_title(node: dict[str, Any]) -> tuple[float, float]:
    """litegraph reports `size` as the body size (no title bar).

    We add the title bar to get the visual bounding box.
    """
    w, h = _effective_size(node)
    return w, h + TITLE_HEIGHT


# A "natural" node size derived from how many slots and widget rows the node
# actually has. Used to clamp absurdly-large nodes that embed an image
# preview (LoadImage/SaveImage), which otherwise dominate the diff canvas.
MAX_NORMAL_WIDTH = 288.0
MAX_NORMAL_HEIGHT = 320.0

# Width that drives the word-wrap budget — intentionally larger than the
# rendered node width so the wrap point doesn't move when we shrink the
# visible body. ``_CHAR_PX`` overestimates real font width by ~10–15%, so
# text computed against this logical width still tends to fit (or only
# slightly overflow) the real rendered rect at ``MAX_NORMAL_WIDTH``.
WRAP_LOGICAL_WIDTH = 360.0


def _natural_size(node: dict[str, Any]) -> tuple[float, float]:
    inputs = node.get("inputs") or []
    outputs = node.get("outputs") or []
    widgets = node.get("widgets_values") or []
    slot_rows = max(len(inputs), len(outputs))
    widget_rows = len(widgets) if isinstance(widgets, list) else (
        len(widgets) if isinstance(widgets, dict) else 0
    )
    h = slot_rows * SLOT_HEIGHT + widget_rows * WIDGET_HEIGHT + 16
    h = max(60.0, h)
    return MAX_NORMAL_WIDTH, h


# Average character width at the body font (Roboto/Arial, font-size 10).
# Used to estimate where to break long widget content.
_CHAR_PX = 5.6
# Vertical pitch for additional lines in a wrapped widget row. Tighter
# than WIDGET_HEIGHT so multi-line rows don't dwarf single-line ones.
_LINE_HEIGHT = 13.0


def _wrap_text(text: str, max_chars: int) -> list[str]:
    """Greedy word-wrap into lines no longer than ``max_chars``.

    Splits on whitespace boundaries; tokens that don't fit on a fresh
    line are split mid-token at ``max_chars`` boundaries (so URLs, file
    paths and JSON blobs without whitespace still wrap).
    """
    if not text:
        return [""]
    if max_chars <= 0:
        return [text]
    import re as _re
    tokens = _re.findall(r"\S+\s*|\s+", text)
    lines: list[str] = []
    current = ""
    for tok in tokens:
        if len(current) + len(tok) <= max_chars:
            current += tok
            continue
        if current:
            lines.append(current.rstrip())
            current = ""
            # Don't carry leading whitespace into the next line.
            tok = tok.lstrip()
        while len(tok) > max_chars:
            lines.append(tok[:max_chars])
            tok = tok[max_chars:]
        current = tok
    if current.rstrip():
        lines.append(current.rstrip())
    return lines or [text]


# Type alias for a single span: (text, fill colour, font weight).
_Span = tuple[str, str, str]


# Widget-name label colour: a muted gray, matching ComfyUI's editor
# convention of dimming the label slightly so the value reads as the
# primary content.
WIDGET_NAME_LABEL = "#9aa0a6"


def _name_prefix(name: str | None) -> str:
    """Render the widget name as a soft-gray label followed by a single
    space (no colon — that's how ComfyUI's own UI shows it)."""
    return f"{name} " if name else ""


def _layout_diff_text(
    name: str | None,
    new_str: str,
    old_str: str,
    max_chars: int,
) -> list[list[_Span]]:
    """Lay out a diff row as a list of rows of spans.

    Single-line policy: ``name NEW  OLD`` (the colours — green for NEW,
    red for OLD — carry the before/after distinction, no arrow needed).

    Multi-line policy: prefix and as much of NEW as fits on the first
    row, the rest of NEW on subsequent rows, then OLD starting on a
    fresh row (so the colour break visually marks the NEW→OLD boundary).
    """
    prefix = _name_prefix(name)
    one_line = f"{prefix}{new_str}  {old_str}"
    if len(one_line) <= max_chars:
        spans: list[_Span] = []
        if prefix:
            spans.append((prefix, WIDGET_NAME_LABEL, "400"))
        spans.append((new_str, "#22c55e", "700"))
        spans.append(("  ", "#888", "400"))
        spans.append((old_str, "#ef4444", "500"))
        return [spans]

    rows: list[list[_Span]] = []
    first_max = max(8, max_chars - len(prefix))
    new_lines = _wrap_text(new_str, first_max)
    first: list[_Span] = []
    if prefix:
        first.append((prefix, WIDGET_NAME_LABEL, "400"))
    first.append((new_lines[0], "#22c55e", "700"))
    rows.append(first)
    for line in new_lines[1:]:
        rows.append([(line, "#22c55e", "700")])
    for line in _wrap_text(old_str, max_chars):
        rows.append([(line, "#ef4444", "500")])
    return rows


def _layout_value_text(
    name: str | None,
    value_str: str,
    max_chars: int,
) -> list[list[_Span]]:
    """Lay out a plain (non-diff) widget row, wrapping if needed."""
    prefix = _name_prefix(name)
    one_line = f"{prefix}{value_str}"
    if len(one_line) <= max_chars:
        spans: list[_Span] = []
        if prefix:
            spans.append((prefix, WIDGET_NAME_LABEL, "400"))
        spans.append((value_str, WIDGET_TEXT, "400"))
        return [spans]
    first_max = max(8, max_chars - len(prefix))
    value_lines = _wrap_text(value_str, first_max)
    rows: list[list[_Span]] = []
    first: list[_Span] = []
    if prefix:
        first.append((prefix, WIDGET_NAME_LABEL, "400"))
    first.append((value_lines[0], WIDGET_TEXT, "400"))
    rows.append(first)
    for line in value_lines[1:]:
        rows.append([(line, WIDGET_TEXT, "400")])
    return rows


def _emit_text_rows(
    out: list[str],
    x: float,
    first_baseline_y: float,
    rows: list[list[_Span]],
    line_height: float,
) -> None:
    """Append SVG <text> elements for the given row list. ``first_baseline_y``
    is the baseline of the first line; subsequent lines step down by
    ``line_height``."""
    for i, spans in enumerate(rows):
        line_y = first_baseline_y + i * line_height
        spans_html = "".join(
            f'<tspan fill="{c}" font-weight="{w}">{_esc(t)}</tspan>'
            for t, c, w in spans
        )
        out.append(
            f'<text x="{x:.1f}" y="{line_y:.1f}" '
            f'font-family="Roboto, Arial, sans-serif" font-size="10">'
            f'{spans_html}</text>'
        )


def clamp_to_natural(node: dict[str, Any]) -> tuple[float, float]:
    """Return the rendering size topo layout uses for the node.

    Width: always the natural width — a uniform value that gives every
    node enough room for wrapping. Editor-saved widths (which can be as
    narrow as ~200 px for collapsed nodes) would otherwise constrain the
    wrap budget far below what looks right alongside other nodes.

    Height: the editor's actual height when reasonable, but clamped to
    the natural height for image-preview nodes that embed a full-size
    preview (LoadImage / SaveImage etc.).

    When the node already carries a ``_render_size`` (set by
    :func:`_apply_topo_layout` after estimating wrapped-row extra
    height), we honour it — that's how the layout sees the *true*
    rendered size and packs sibling nodes below it without overlap.
    """
    rs = node.get("_render_size")
    if isinstance(rs, (list, tuple)) and len(rs) == 2:
        try:
            return float(rs[0]), float(rs[1])
        except (TypeError, ValueError):
            pass
    _, h = _normalize_size(node)
    nat_w, nat_h = _natural_size(node)
    new_h = nat_h if h > nat_h * 1.5 else h
    return nat_w, new_h


def _wrapped_widget_extra_height(
    node: dict[str, Any],
    changed_widgets: list[dict[str, Any]] | None,
    width: float,
) -> float:
    """Estimate extra vertical space the node needs because some widget
    rows will wrap to multiple lines at the given ``width``.

    Returns ``0.0`` when every row fits in one line. Used by the topo
    layout path to grow the node body so wrapped rows aren't clipped.
    """
    widgets_values = node.get("widgets_values")
    if not isinstance(widgets_values, list):
        return 0.0
    widget_names = widget_names_for_node(node) or []
    changed_by_idx: dict[int, dict[str, Any]] = {}
    for rec in changed_widgets or []:
        if isinstance(rec, dict) and "index" in rec:
            changed_by_idx[rec["index"]] = rec

    # Use the same logical wrap width as the renderer — otherwise the
    # row-count estimate would diverge from what _render_node actually
    # produces and the reserved height would be wrong.
    max_chars = max(10, int((WRAP_LOGICAL_WIDTH - 24) / _CHAR_PX))
    extra = 0.0
    for i, value in enumerate(widgets_values):
        # Mirror the renderer's structural-skip rule so the height
        # estimate matches what actually gets drawn.
        if i < len(widget_names) and is_structural_name(widget_names[i]):
            continue
        wname = widget_names[i] if i < len(widget_names) else None
        rec = changed_by_idx.get(i)
        if rec:
            new_s = _format_widget_value(rec.get("new"), max_len=None)
            old_s = _format_widget_value(rec.get("old"), max_len=None)
            rows = _layout_diff_text(wname, new_s, old_s, max_chars)
        else:
            value_s = _format_widget_value(value, max_len=None)
            rows = _layout_value_text(wname, value_s, max_chars)
        if len(rows) > 1:
            extra += (len(rows) - 1) * _LINE_HEIGHT
    return extra


def _effective_size(node: dict[str, Any]) -> tuple[float, float]:
    """Size used by the renderer for one node.

    By default, returns the node's actual ``size`` field unchanged — so the
    ``preserve`` layout mode reflects the workflow's editor view exactly.

    The topo layout path opts into clamping by writing a tuple under the
    ``_render_size`` key on the (already-copied) node. This is the single
    in-band signal the renderer respects; there is no module-level toggle.
    """
    rs = node.get("_render_size")
    if isinstance(rs, (list, tuple)) and len(rs) == 2:
        try:
            return float(rs[0]), float(rs[1])
        except (TypeError, ValueError):
            pass
    return _normalize_size(node)


def _slot_color(slot_type: Any) -> str:
    if not isinstance(slot_type, str):
        return SLOT_DEFAULT
    return SLOT_COLORS.get(slot_type.upper(), SLOT_DEFAULT)


def _input_slot_pos(node: dict[str, Any], slot_idx: int) -> tuple[float, float]:
    x, y = _normalize_pos(node)
    return x, y + TITLE_HEIGHT + slot_idx * SLOT_HEIGHT + SLOT_HEIGHT / 2


def _output_slot_pos(node: dict[str, Any], slot_idx: int) -> tuple[float, float]:
    x, y = _normalize_pos(node)
    w, _ = _effective_size(node)
    return x + w, y + TITLE_HEIGHT + slot_idx * SLOT_HEIGHT + SLOT_HEIGHT / 2


def _bezier_path(x1: float, y1: float, x2: float, y2: float) -> str:
    cp_offset = max(abs(x2 - x1) * 0.5, 50.0)
    return f"M {x1:.1f} {y1:.1f} C {x1 + cp_offset:.1f} {y1:.1f}, {x2 - cp_offset:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}"


def _format_widget_value(v: Any, max_len: int | None = 32) -> str:
    """Render a widget value as a string.

    ``max_len`` is the string-truncation cap (an ellipsis is appended past
    it); pass ``None`` to disable truncation. With ``max_len=None`` dict
    and list/tuple values are expanded as compact JSON instead of the
    placeholder ``{N keys}`` / ``[N items]`` form — useful when the
    renderer is going to word-wrap and we want the real content.
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return f"{v}"
    if isinstance(v, str):
        s = v.replace("\n", " ⏎ ")
        if max_len is None or len(s) <= max_len:
            return s
        return s[: max_len - 1] + "…"
    if isinstance(v, (list, tuple, dict)):
        if max_len is None:
            import json as _json
            try:
                return _json.dumps(v, ensure_ascii=False, default=str, separators=(", ", ": "))
            except (TypeError, ValueError):
                return str(v)
        if isinstance(v, dict):
            return f"{{{len(v)} keys}}"
        return f"[{len(v)} items]"
    return str(v)


# --------------------------------------------------------------------- node
def _render_node(
    node: dict[str, Any],
    *,
    border: str | None = None,
    border_width: float = 1.0,
    body_overlay: str | None = None,
    label_badge: str | None = None,
    badge_color: str = DIFF_CHANGED,
    changed_widgets: Iterable[dict[str, Any]] = (),
    show_widgets: bool = True,
) -> list[str]:
    """Emit SVG elements for a single litegraph node.

    ``changed_widgets`` is the rich diff payload — each item is
    ``{index, name, old, new}``. The renderer highlights the matching
    widget row and overlays the old value in red strike-through after
    the new value in green.
    """
    x, y = _normalize_pos(node)
    w, _ = _effective_size(node)
    _, h_total = _size_with_title(node)
    title = node.get("title") or node.get("type") or "node"
    node_type = node.get("type") or "unknown"
    mode = node.get("mode", 0)

    # Body bg differs for bypass / mute.
    bg = NODE_BG
    if mode == 4:
        bg = NODE_BG_BYPASSED
    elif mode == 2:
        bg = NODE_BG_MUTED

    title_bg = node.get("bgcolor") or TITLE_BG_DEFAULT
    color = node.get("color") or NODE_BORDER

    out: list[str] = []
    out.append(f'<g class="node" data-id="{_esc(str(node.get("id")))}" data-type="{_esc(node_type)}">')

    # Outer body
    stroke = border or color
    out.append(
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h_total:.1f}" '
        f'rx="{NODE_RADIUS}" ry="{NODE_RADIUS}" fill="{bg}" stroke="{stroke}" '
        f'stroke-width="{border_width:.1f}" />'
    )

    # Title bar (top portion of the rounded rect)
    out.append(
        f'<path d="M {x + NODE_RADIUS:.1f} {y:.1f} '
        f'L {x + w - NODE_RADIUS:.1f} {y:.1f} '
        f'Q {x + w:.1f} {y:.1f} {x + w:.1f} {y + NODE_RADIUS:.1f} '
        f'L {x + w:.1f} {y + TITLE_HEIGHT:.1f} '
        f'L {x:.1f} {y + TITLE_HEIGHT:.1f} '
        f'L {x:.1f} {y + NODE_RADIUS:.1f} '
        f'Q {x:.1f} {y:.1f} {x + NODE_RADIUS:.1f} {y:.1f} Z" '
        f'fill="{title_bg}" />'
    )
    out.append(
        f'<text x="{x + 10:.1f}" y="{y + TITLE_HEIGHT - 9:.1f}" '
        f'fill="{TITLE_TEXT}" font-family="Roboto, Arial, sans-serif" font-size="13" '
        f'font-weight="600">{_esc(str(title))}</text>'
    )

    # Slots
    inputs = node.get("inputs") or []
    outputs = node.get("outputs") or []

    for idx, inp in enumerate(inputs):
        cx, cy = _input_slot_pos(node, idx)
        col = _slot_color(inp.get("type"))
        connected = inp.get("link") is not None
        fill = col if connected else NODE_BG
        out.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{SLOT_RADIUS}" '
            f'fill="{fill}" stroke="{col}" stroke-width="1.5" />'
        )
        label = inp.get("name") or inp.get("type") or ""
        out.append(
            f'<text x="{cx + SLOT_LABEL_PAD:.1f}" y="{cy + 3.5:.1f}" '
            f'fill="{SLOT_TEXT}" font-family="Roboto, Arial, sans-serif" font-size="11">'
            f'{_esc(str(label))}</text>'
        )

    for idx, outp in enumerate(outputs):
        cx, cy = _output_slot_pos(node, idx)
        col = _slot_color(outp.get("type"))
        connected = bool(outp.get("links"))
        fill = col if connected else NODE_BG
        out.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{SLOT_RADIUS}" '
            f'fill="{fill}" stroke="{col}" stroke-width="1.5" />'
        )
        label = outp.get("name") or outp.get("type") or ""
        out.append(
            f'<text x="{cx - SLOT_LABEL_PAD:.1f}" y="{cy + 3.5:.1f}" '
            f'fill="{SLOT_TEXT}" font-family="Roboto, Arial, sans-serif" font-size="11" '
            f'text-anchor="end">{_esc(str(label))}</text>'
        )

    # Widget rows. Each row's height is variable — multi-line wrapping
    # is allowed both for diff rows (NEW + OLD shown in full) and for
    # plain rows (long dicts / strings shown in full). The topo layout
    # pre-computes the extra height per node so the body is tall enough;
    # outside topo mode, overflow may run past the node's bottom edge.
    if show_widgets:
        widgets_values = node.get("widgets_values") or []
        # widget_names_for_node already consults `node._widget_names` first.
        widget_names = widget_names_for_node(node) or []
        slot_count = max(len(inputs), len(outputs))
        widget_y = y + TITLE_HEIGHT + slot_count * SLOT_HEIGHT + 4
        changed_lookup: dict[int, dict[str, Any]] = {}
        for rec in changed_widgets:
            if isinstance(rec, dict) and "index" in rec:
                changed_lookup[rec["index"]] = rec

        # Wrap budget is computed against WRAP_LOGICAL_WIDTH (not the actual
        # rendered width `w`) so the wrap point stays put when the visible
        # node body is shrunk. _CHAR_PX overestimates real font width
        # enough that text laid out for the logical width tends to still
        # fit the smaller real rect; if it does overflow slightly, the
        # extra pixels just bleed into the inter-column gap.
        max_chars = max(10, int((WRAP_LOGICAL_WIDTH - 24) / _CHAR_PX))
        cursor_y = widget_y
        for i, value in enumerate(_iter_widgets(widgets_values)):
            rec = changed_lookup.get(i)
            # Widget name comes from the registry (when ComfyUI is running)
            # or from the API adapter via _widget_names. For changed rows
            # the diff record carries the name too — use it as a last
            # resort so common UI nodes still get labelled.
            wname = widget_names[i] if i < len(widget_names) else None
            if rec and not wname:
                wname = rec.get("name") or None
            # Skip structural / control_after_generate placeholders so
            # noise like LoadImage's "image" sentinel or rgthree Seed's
            # hidden button nulls don't get a visible widget row. We
            # only skip when we have enough names to know it's noise —
            # if widget_names is shorter than widgets_values (no info
            # for this position), fall back to rendering it.
            if i < len(widget_names) and is_structural_name(widget_names[i]):
                continue
            if rec:
                new_s = _format_widget_value(rec.get("new"), max_len=None)
                old_s = _format_widget_value(rec.get("old"), max_len=None)
                rows = _layout_diff_text(wname, new_s, old_s, max_chars)
                row_fill = DIFF_CHANGED
                row_alpha = 0.30
            else:
                value_s = _format_widget_value(value, max_len=None)
                rows = _layout_value_text(wname, value_s, max_chars)
                row_fill = WIDGET_BG
                row_alpha = 1.0
            row_h = WIDGET_HEIGHT + (len(rows) - 1) * _LINE_HEIGHT
            # Stop if we'd draw past the node body (only happens in preserve
            # mode — topo layout reserves the full required height).
            if cursor_y + row_h > y + h_total - 4:
                break
            out.append(
                f'<rect x="{x + 8:.1f}" y="{cursor_y:.1f}" width="{w - 16:.1f}" '
                f'height="{row_h - 4:.1f}" rx="4" ry="4" '
                f'fill="{row_fill}" fill-opacity="{row_alpha}" '
                f'stroke="{WIDGET_OUTLINE}" stroke-width="0.5" />'
            )
            _emit_text_rows(out, x + 14, cursor_y + WIDGET_HEIGHT - 8, rows, _LINE_HEIGHT)
            cursor_y += row_h

    # Diff body overlay (translucent fill across whole node)
    if body_overlay:
        out.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h_total:.1f}" '
            f'rx="{NODE_RADIUS}" ry="{NODE_RADIUS}" fill="{body_overlay}" '
            f'fill-opacity="0.18" pointer-events="none" />'
        )

    # Diff badge in top-right corner
    if label_badge:
        bx = x + w - 6
        by = y + 6
        badge_text = label_badge
        badge_w = 10 + len(badge_text) * 6
        out.append(
            f'<rect x="{bx - badge_w:.1f}" y="{by:.1f}" width="{badge_w:.1f}" '
            f'height="16" rx="4" ry="4" fill="{badge_color}" />'
        )
        out.append(
            f'<text x="{bx - badge_w / 2:.1f}" y="{by + 12:.1f}" fill="#111" '
            f'font-family="Roboto, Arial, sans-serif" font-size="10" font-weight="700" '
            f'text-anchor="middle">{_esc(badge_text)}</text>'
        )

    out.append("</g>")
    return out


def _iter_widgets(widgets_values: Any) -> list[Any]:
    if isinstance(widgets_values, dict):
        return list(widgets_values.values())
    if isinstance(widgets_values, list):
        return widgets_values
    return []


# --------------------------------------------------------------------- link
def _render_link(
    link: list[Any],
    node_lookup: dict[str, dict[str, Any]],
    *,
    override_color: str | None = None,
    dashed: bool = False,
    width: float = 2.5,
) -> str | None:
    if not isinstance(link, (list, tuple)) or len(link) < 6:
        return None
    _, src_id, src_slot, dst_id, dst_slot, link_type = link[:6]
    src = node_lookup.get(str(src_id))
    dst = node_lookup.get(str(dst_id))
    if not src or not dst:
        return None
    try:
        sx, sy = _output_slot_pos(src, int(src_slot or 0))
        ex, ey = _input_slot_pos(dst, int(dst_slot or 0))
    except (TypeError, ValueError):
        return None
    color = override_color or _slot_color(link_type)
    dash_attr = ' stroke-dasharray="6 4"' if dashed else ""
    return (
        f'<path d="{_bezier_path(sx, sy, ex, ey)}" fill="none" '
        f'stroke="{color}" stroke-width="{width}" stroke-linecap="round"{dash_attr} />'
    )


# ------------------------------------------------------------------ canvas
def _compute_bounds(nodes: Iterable[dict[str, Any]]) -> tuple[float, float, float, float]:
    xs_min, ys_min, xs_max, ys_max = [], [], [], []
    for node in nodes:
        x, y = _normalize_pos(node)
        # _size_with_title reads _render_size (if set by topo layout) or
        # the node's actual size — both branches are correct here.
        w, h = _size_with_title(node)
        xs_min.append(x)
        ys_min.append(y)
        xs_max.append(x + w)
        ys_max.append(y + h)
    if not xs_min:
        return 0.0, 0.0, 800.0, 600.0
    return min(xs_min), min(ys_min), max(xs_max), max(ys_max)


def _grid_background(x0: float, y0: float, w: float, h: float) -> list[str]:
    return [
        f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{w:.1f}" height="{h:.1f}" fill="{BG_COLOR}" />',
        '<defs>'
        '<pattern id="lg-grid" width="50" height="50" patternUnits="userSpaceOnUse">'
        f'<path d="M 50 0 L 0 0 0 50" fill="none" stroke="{GRID_COLOR}" stroke-width="1"/>'
        '</pattern></defs>',
        f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{w:.1f}" height="{h:.1f}" fill="url(#lg-grid)" />',
    ]


def _legend(x: float, y: float) -> list[str]:
    items = [
        ("Added", DIFF_ADDED),
        ("Removed", DIFF_REMOVED),
        ("Changed", DIFF_CHANGED),
        ("Moved", DIFF_MOVED),
    ]
    out = [
        f'<rect x="{x:.1f}" y="{y:.1f}" width="320" height="36" rx="6" ry="6" '
        f'fill="#000" fill-opacity="0.55" stroke="#444" />'
    ]
    cx = x + 12
    for label, color in items:
        out.append(
            f'<rect x="{cx:.1f}" y="{y + 10:.1f}" width="14" height="14" rx="3" '
            f'fill="{color}" />'
        )
        out.append(
            f'<text x="{cx + 20:.1f}" y="{y + 22:.1f}" fill="#FFF" font-size="12" '
            f'font-family="Roboto, Arial, sans-serif">{label}</text>'
        )
        cx += 80
    return out


# ----------------------------------------------------------------- public
def _apply_topo_layout(
    workflow_a: dict[str, Any],
    workflow_b: dict[str, Any],
    diff: dict[str, Any],
) -> dict[str, Any]:
    """Return a copy of `diff` with every visible node's `pos` overridden by
    the topological layered layout computed on the union graph.

    Only the nodes referenced by the (already-filtered) diff are positioned —
    so when called after `_filter_diff_changed_only`, the layout naturally
    compacts to only the visible nodes.
    """
    # Collect the nodes we actually want to position (the diff's view).
    visible_nodes: list[dict[str, Any]] = []
    for c in diff["common_nodes"]:
        visible_nodes.append(c["b"])
    visible_nodes.extend(diff["added_nodes"])
    visible_nodes.extend(diff["removed_nodes"])

    if not visible_nodes:
        return diff

    visible_ids: set[str] = {str(n.get("id")) for n in visible_nodes}

    # Only keep links between visible nodes for layering.
    def _filter_links(links: list[Any]) -> list[Any]:
        out = []
        for link in links or []:
            if not isinstance(link, (list, tuple)) or len(link) < 5:
                continue
            if str(link[1]) in visible_ids and str(link[3]) in visible_ids:
                out.append(link)
        return out

    links_for_layout = (
        _filter_links(workflow_a.get("links"))
        + _filter_links(workflow_b.get("links"))
    )

    # node-id -> list-of-change-records (only common nodes can have these)
    changed_by_id: dict[str, list[dict[str, Any]]] = {}
    for c in diff["common_nodes"]:
        recs = c["changed_widgets"]
        if recs:
            changed_by_id[str(c["b"].get("id"))] = recs

    # node-id -> widget names from the A side, used as a fallback when the
    # rendered side (B for common, the node itself for added/removed)
    # has no resolved names. This is how widget rows on common UI nodes
    # get labelled when the A side is API-format.
    a_widget_names: dict[str, list[str]] = {}
    for c in diff["common_nodes"]:
        names = widget_names_for_node(c["a"])
        if names:
            a_widget_names[str(c["b"].get("id"))] = names

    # class_type -> widget names registry, built from any node on either
    # side that already carries names (typically API-format nodes via the
    # adapter). Lets added B-only nodes — which are UI-format and have
    # no live ComfyUI registry to consult — borrow names from same-type
    # nodes on the A side. Covers e.g. an added 'Seed (rgthree)' in B
    # whose name comes from a removed/common 'Seed (rgthree)' in A.
    type_widget_names: dict[str, list[str]] = {}
    for src in (workflow_a, workflow_b):
        for n in src.get("nodes") or []:
            ntype = n.get("type")
            if not isinstance(ntype, str) or ntype in type_widget_names:
                continue
            names = widget_names_for_node(n)
            if names:
                type_widget_names[ntype] = names

    # ---- pass 1: build sized copies (positions still unknown) ----
    # Each copy carries `_render_size` reflecting the *real* rendered size
    # (including extra height for wrapped widget rows) AND `_widget_names`
    # backfilled from the A side when missing. Feeding these into the
    # layout means later nodes in the same column won't overlap earlier
    # ones that grew vertically due to wrapping.
    sized: dict[str, dict[str, Any]] = {}

    def _make_sized_copy(
        node: dict[str, Any],
        changed: list[dict[str, Any]] | None,
        a_names: list[str] | None,
    ) -> dict[str, Any]:
        copy = dict(node)
        if not widget_names_for_node(copy):
            # First try the specific A-side counterpart (matches by id, so
            # the names are guaranteed to belong to *this exact node*).
            if a_names:
                copy["_widget_names"] = list(a_names)
            else:
                # Fall back to the cross-type registry: any same-type node
                # on either side that already has names.
                ntype = copy.get("type")
                if isinstance(ntype, str):
                    reg_names = type_widget_names.get(ntype)
                    if reg_names:
                        copy["_widget_names"] = list(reg_names)
        cw, ch = clamp_to_natural(copy)
        ch += _wrapped_widget_extra_height(copy, changed, cw)
        copy["_render_size"] = [cw, ch]
        return copy

    for c in diff["common_nodes"]:
        nid = str(c["b"].get("id"))
        sized[nid] = _make_sized_copy(
            c["b"], changed_by_id.get(nid), a_widget_names.get(nid)
        )
    for n in diff["added_nodes"]:
        nid = str(n.get("id"))
        sized[nid] = _make_sized_copy(n, None, None)
    for n in diff["removed_nodes"]:
        nid = str(n.get("id"))
        if nid not in sized:
            sized[nid] = _make_sized_copy(n, None, None)

    # ---- pass 2: layout with real sizes ----
    sized_list = list(sized.values())
    positions = layered_layout_union(sized_list, [], sized_list, links_for_layout)

    def _placed(nid: str) -> dict[str, Any] | None:
        node = sized.get(nid)
        if node is None or nid not in positions:
            return None
        x, y = positions[nid]
        node = dict(node)
        node["pos"] = [x, y]
        return node

    def _override(node: dict[str, Any]) -> dict[str, Any]:
        placed = _placed(str(node.get("id")))
        return placed if placed is not None else node

    new_common = []
    for c in diff["common_nodes"]:
        new_c = dict(c)
        new_c["b"] = _override(c["b"])
        # The A node keeps its identity but borrows the laid-out position
        # so any downstream code that touches it sees a consistent view.
        a_placed = _placed(str(c["b"].get("id")))
        new_c["a"] = a_placed if a_placed is not None else c["a"]
        new_common.append(new_c)

    return {
        "added_nodes": [_override(n) for n in diff["added_nodes"]],
        "removed_nodes": [_override(n) for n in diff["removed_nodes"]],
        "common_nodes": new_common,
        "added_links": list(diff["added_links"]),
        "removed_links": list(diff["removed_links"]),
        "common_links": list(diff["common_links"]),
    }


def _filter_diff_changed_only(
    diff: dict[str, Any], *, include_moved: bool
) -> dict[str, Any]:
    """Return a diff dict containing only nodes/links that participate in a change.

    A "change" is widget-value change, type change, addition, or removal.
    Pure move/resize is included only if ``include_moved`` is True.
    Links are kept only if both endpoints are visible.
    """
    visible_ids: set[str] = set()
    kept_common: list[dict[str, Any]] = []

    for n in diff["added_nodes"]:
        visible_ids.add(str(n.get("id")))
    for n in diff["removed_nodes"]:
        visible_ids.add(str(n.get("id")))

    for c in diff["common_nodes"]:
        has_value_change = bool(c["changed_widgets"]) or c["type_changed"]
        if has_value_change or (include_moved and c["moved"]):
            kept_common.append(c)
            visible_ids.add(str(c["b"].get("id")))

    def link_visible(link: Any) -> bool:
        if not isinstance(link, (list, tuple)) or len(link) < 5:
            return False
        return str(link[1]) in visible_ids and str(link[3]) in visible_ids

    return {
        "added_nodes": diff["added_nodes"],
        "removed_nodes": diff["removed_nodes"],
        "common_nodes": kept_common,
        "added_links": [l for l in diff["added_links"] if link_visible(l)],
        "removed_links": [l for l in diff["removed_links"] if link_visible(l)],
        "common_links": [l for l in diff["common_links"] if link_visible(l)],
    }


def render_workflow_svg(workflow: dict[str, Any], *, padding: int = 80) -> str:
    """Render a single workflow as SVG, no diff annotations."""
    nodes = list(workflow.get("nodes") or [])
    links = list(workflow.get("links") or [])
    node_lookup = {str(n.get("id")): n for n in nodes}

    x0, y0, x1, y1 = _compute_bounds(nodes)
    x0 -= padding
    y0 -= padding
    x1 += padding
    y1 += padding
    w, h = x1 - x0, y1 - y0

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:.1f} {y0:.1f} {w:.1f} {h:.1f}" '
        f'width="{w:.0f}" height="{h:.0f}">'
    )
    parts.extend(_grid_background(x0, y0, w, h))
    for link in links:
        s = _render_link(link, node_lookup)
        if s:
            parts.append(s)
    for node in nodes:
        parts.extend(_render_node(node))
    parts.append("</svg>")
    return "".join(parts)


def render_workflow_diff_svg(
    workflow_a: dict[str, Any],
    workflow_b: dict[str, Any],
    diff: dict[str, Any],
    *,
    mode: str = "all",
    include_moved: bool = False,
    layout: str = "topo",
    padding: int = 80,
) -> str:
    """Render two workflows as a single annotated SVG diff.

    ``mode``
        ``"all"`` (default) renders every node — added, removed, common —
        so you get the full graph for context.  ``"changed_only"`` keeps
        only nodes that actually changed (added / removed / widget-changed
        / type-changed). Unchanged common nodes are omitted.

    ``include_moved``
        Only meaningful with ``mode="changed_only"``. When ``False``
        (default) nodes whose only change is position/size are also
        omitted, which is what you usually want when scanning for
        semantic differences. When ``True`` they are included and the
        blue MOVED border / badge is drawn.

    ``layout``
        ``"topo"`` (default) re-lays the graph out with a topological
        layered layout — depth from a root determines the column, nodes
        within a column stack vertically. Eliminates large empty bands
        that the editor-saved positions sometimes leave behind.
        ``"preserve"`` keeps the original ``pos`` field from each node,
        which is closer to how the workflow looked in the ComfyUI
        editor.

    With ``"preserve"``:
        - Common and added nodes are drawn at their position from
          workflow_b (the 'after' state).
        - Removed nodes are drawn at their position from workflow_a.
    With ``"topo"``:
        - Positions are recomputed from the union graph so the same node
          id occupies the same coordinate on both sides.
    """
    if mode not in ("all", "changed_only"):
        raise ValueError(f'mode must be "all" or "changed_only", got {mode!r}')
    if layout not in ("topo", "preserve"):
        raise ValueError(f'layout must be "topo" or "preserve", got {layout!r}')
    if mode == "changed_only":
        diff = _filter_diff_changed_only(diff, include_moved=include_moved)

    if layout == "topo":
        diff = _apply_topo_layout(workflow_a, workflow_b, diff)
    # Build the unified node lookup that links will resolve through.
    node_lookup: dict[str, dict[str, Any]] = {}
    all_render_nodes: list[tuple[dict[str, Any], str]] = []  # (node, status)

    for common in diff["common_nodes"]:
        node_lookup[str(common["b"].get("id"))] = common["b"]
        all_render_nodes.append((common["b"], "common"))

    for node in diff["added_nodes"]:
        node_lookup[str(node.get("id"))] = node
        all_render_nodes.append((node, "added"))

    # Removed nodes are added to the lookup ONLY if their id doesn't already
    # exist (a removed node always has an A-only id, but be defensive).
    for node in diff["removed_nodes"]:
        nid = str(node.get("id"))
        if nid not in node_lookup:
            node_lookup[nid] = node
        all_render_nodes.append((node, "removed"))

    # Bounds across both workflows
    x0, y0, x1, y1 = _compute_bounds([n for n, _ in all_render_nodes])
    x0 -= padding
    y0 -= padding
    x1 += padding
    y1 += padding
    w, h = x1 - x0, y1 - y0

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:.1f} {y0:.1f} {w:.1f} {h:.1f}" '
        f'width="{w:.0f}" height="{h:.0f}">'
    )
    parts.extend(_grid_background(x0, y0, w, h))

    # --- Links: removed (red dashed) first, then common (normal), then added (green)
    for link in diff["removed_links"]:
        s = _render_link(link, node_lookup, override_color=DIFF_REMOVED, dashed=True, width=3.0)
        if s:
            parts.append(s)
    for link in diff["common_links"]:
        s = _render_link(link, node_lookup)
        if s:
            parts.append(s)
    for link in diff["added_links"]:
        s = _render_link(link, node_lookup, override_color=DIFF_ADDED, width=3.0)
        if s:
            parts.append(s)

    # --- Nodes
    for common in diff["common_nodes"]:
        node = common["b"]
        changed_widgets = common["changed_widgets"]
        moved = common["moved"]
        type_changed = common["type_changed"]
        if type_changed:
            border = DIFF_CHANGED
            badge = "TYPE"
        elif changed_widgets:
            border = DIFF_CHANGED
            badge = "Δ" if not moved else "Δ MOVED"
        elif moved:
            border = DIFF_MOVED
            badge = "MOVED"
        else:
            border = None
            badge = None
        parts.extend(
            _render_node(
                node,
                border=border,
                border_width=2.5 if border else 1.0,
                label_badge=badge,
                badge_color=DIFF_CHANGED if border == DIFF_CHANGED else DIFF_MOVED,
                changed_widgets=changed_widgets,
            )
        )

    for node in diff["added_nodes"]:
        parts.extend(
            _render_node(
                node,
                border=DIFF_ADDED,
                border_width=3.0,
                body_overlay=DIFF_ADDED,
                label_badge="+",
                badge_color=DIFF_ADDED,
            )
        )

    for node in diff["removed_nodes"]:
        parts.extend(
            _render_node(
                node,
                border=DIFF_REMOVED,
                border_width=3.0,
                body_overlay=DIFF_REMOVED,
                label_badge="−",
                badge_color=DIFF_REMOVED,
            )
        )

    # Legend in screen space (top-left of viewBox)
    parts.extend(_legend(x0 + 16, y0 + 16))

    parts.append("</svg>")
    return "".join(parts)
