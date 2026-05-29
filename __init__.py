"""
ComfyUI Workflow Diff — custom node providing:

    POST /workflow/diff
        Render an annotated visual diff between two workflows as SVG.
        Body: {"workflow_a": <wf>, "workflow_b": <wf>,
               "mode": "all"|"changed_only",
               "layout": "topo"|"preserve",
               "include_moved": bool}

    GET  /workflow/diff
        Returns documentation for the diff endpoint.

    GET  /workflow/diff/ui
        Browser-friendly HTML page that POSTs two pasted workflows and
        shows the rendered SVG diff inline (handy for manual testing).

    POST /workflow/convert
        Convert a 'full' workflow (UI graph format, with nodes & links)
        to the API prompt format used by /prompt.  Ported from
        https://github.com/SethRobinson/comfyui-workflow-to-api-converter-endpoint
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from .format_adapter import detect_format, prepare_diff_inputs
from .workflow_diff import diff_workflows
from .workflow_renderer import render_workflow_diff_svg, render_workflow_svg

logger = logging.getLogger(__name__)

__version__ = "0.1.0"

MAX_CONTENT_LENGTH = 4 * 1024 * 1024  # 4 MB — diff bodies carry 2 workflows


# The converter and ComfyUI's PromptServer are only available inside a
# running ComfyUI process. Guard them so this package is still importable
# from outside ComfyUI (tests, ad-hoc tooling, type checkers). When the
# guards trip, route registration is skipped entirely — the package
# becomes a no-op rather than failing at import.
WorkflowConverter: type | None
try:
    from .workflow_converter import WorkflowConverter as _WorkflowConverter
    WorkflowConverter = _WorkflowConverter
except ImportError as e:
    logger.warning("workflow_converter unavailable (%s); /workflow/convert will be disabled.", e)
    WorkflowConverter = None

try:
    from server import PromptServer
except ImportError as e:
    logger.warning(
        "PromptServer unavailable (%s); HTTP endpoints will NOT be registered. "
        "This is expected when importing this package outside of ComfyUI.", e,
    )
    PromptServer = None


# --------------------------------------------------------------- /convert
async def convert_workflow_endpoint(request: web.Request) -> web.Response:
    """Convert a non-API workflow to API format (compatible with /prompt)."""
    if WorkflowConverter is None:
        return web.json_response(
            {"error": "Workflow converter unavailable — ComfyUI nodes module not loaded."},
            status=503,
        )
    try:
        if request.content_length is not None and request.content_length > MAX_CONTENT_LENGTH:
            return web.json_response(
                {"error": f"Request too large. Max {MAX_CONTENT_LENGTH // (1024 * 1024)} MB"},
                status=413,
            )

        data = await request.json()

        if WorkflowConverter.is_api_format(data):
            return web.json_response(
                data,
                dumps=lambda x: json.dumps(x, ensure_ascii=False, indent=2),
            )

        err = _validate_workflow(data, name="workflow")
        if err:
            return web.json_response({"error": err}, status=400)

        api_format = WorkflowConverter.convert_to_api(data)
        logger.info(
            f"[workflow-diff v{__version__}] converted workflow: "
            f"{len(data['nodes'])} nodes → {len(api_format)} API nodes"
        )
        return web.json_response(
            api_format, dumps=lambda x: json.dumps(x, ensure_ascii=False, indent=2)
        )

    except json.JSONDecodeError as e:
        return web.json_response({"error": f"Invalid JSON: {e}"}, status=400)
    except Exception:
        logger.exception("Error converting workflow")
        return web.json_response({"error": "Internal server error during conversion"}, status=500)


async def converter_info(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "name": "ComfyUI Workflow Diff — convert",
            "version": __version__,
            "description": (
                "POST a non-API workflow JSON here to receive the API-format "
                "(workflow_api) equivalent — same shape as ComfyUI's 'Save (API)'."
            ),
        }
    )


# ----------------------------------------------------------------- /diff
async def diff_workflow_endpoint(request: web.Request) -> web.Response:
    """Render a visual diff between two workflows as SVG.

    Both ``workflow_a`` and ``workflow_b`` may be in either format
    (the full UI workflow JSON with ``nodes``/``links``, or the
    ``workflow_api`` prompt JSON keyed by node id). The format of each
    side is auto-detected from the input shape and coerced to UI
    format internally — the detected format is reported back in the
    ``X-Workflow-Diff-Format-A`` / ``-B`` response headers.

    Body shape (JSON):
        {
            "workflow_a":    <workflow json>,         # required
            "workflow_b":    <workflow json>,         # required
            "mode":          "all" | "changed_only",  # optional, default "all"
            "layout":        "topo" | "preserve",     # optional, default "topo"
                                                      #   ('preserve' only honoured
                                                      #    when both inputs are UI)
            "include_moved": true | false,            # optional, default false
                                                      #   (only meaningful when
                                                      #    mode == "changed_only")
            "stats_only":    true                     # optional, return JSON summary
        }
    """
    try:
        if request.content_length is not None and request.content_length > MAX_CONTENT_LENGTH:
            return web.json_response(
                {"error": f"Request too large. Max {MAX_CONTENT_LENGTH // (1024 * 1024)} MB"},
                status=413,
            )
        body = await request.json()
    except json.JSONDecodeError as e:
        return web.json_response({"error": f"Invalid JSON: {e}"}, status=400)

    raw_a = body.get("workflow_a")
    raw_b = body.get("workflow_b")
    if not isinstance(raw_a, dict) or not isinstance(raw_b, dict):
        return web.json_response(
            {"error": "Both 'workflow_a' and 'workflow_b' must be workflow JSON objects."},
            status=400,
        )

    try:
        wf_a, wf_b, fmt_a, fmt_b = prepare_diff_inputs(raw_a, raw_b)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception:
        logger.exception("Error coercing workflows")
        return web.json_response({"error": "Internal server error during input coercion"}, status=500)

    mode = (body.get("mode") or "all").lower()
    if mode not in ("all", "changed_only"):
        return web.json_response(
            {"error": 'mode must be "all" or "changed_only"'}, status=400
        )
    include_moved = bool(body.get("include_moved", False))
    layout = (body.get("layout") or "topo").lower()
    if layout not in ("topo", "preserve"):
        return web.json_response(
            {"error": 'layout must be "topo" or "preserve"'}, status=400
        )

    # 'preserve' only makes sense when both inputs are full UI workflows
    # — that's the only situation where the node `pos` fields represent
    # actual editor-authored positions. For anything else (any side is
    # API-format), force topo and tell the caller via a header.
    layout_overridden = False
    if layout == "preserve" and (fmt_a, fmt_b) != ("ui", "ui"):
        layout = "topo"
        layout_overridden = True

    try:
        diff = diff_workflows(wf_a, wf_b)
    except Exception:
        logger.exception("Error diffing workflows")
        return web.json_response({"error": "Internal server error during diff"}, status=500)

    extra_headers: dict[str, str] = {
        "X-Workflow-Diff-Format-A": fmt_a,
        "X-Workflow-Diff-Format-B": fmt_b,
    }
    if layout_overridden:
        extra_headers["X-Workflow-Diff-Notice"] = (
            "layout forced to 'topo' — 'preserve' is only valid when both "
            "inputs are UI-format workflows (only those carry editor positions)"
        )

    if body.get("stats_only"):
        return web.json_response(
            {
                "format_a": fmt_a,
                "format_b": fmt_b,
                "layout_used": layout,
                "added_nodes": len(diff["added_nodes"]),
                "removed_nodes": len(diff["removed_nodes"]),
                "common_nodes": len(diff["common_nodes"]),
                "changed_nodes": sum(
                    1
                    for c in diff["common_nodes"]
                    if c["changed_widgets"] or c["type_changed"] or c["moved"]
                ),
                "added_links": len(diff["added_links"]),
                "removed_links": len(diff["removed_links"]),
                "common_links": len(diff["common_links"]),
            },
            headers=extra_headers,
        )

    try:
        svg_text = render_workflow_diff_svg(
            wf_a, wf_b, diff,
            mode=mode,
            include_moved=include_moved,
            layout=layout,
        )
    except Exception:
        logger.exception("Error rendering diff SVG")
        return web.json_response({"error": "Internal server error during render"}, status=500)

    return web.Response(text=svg_text, content_type="image/svg+xml", headers=extra_headers)


async def diff_info(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "name": "ComfyUI Workflow Diff",
            "version": __version__,
            "description": (
                "POST {workflow_a, workflow_b, mode?, layout?, include_moved?} "
                "to receive an annotated litegraph-style SVG diff. Both workflows "
                "can be in either format — full UI workflow JSON (with "
                "'nodes'/'links') or workflow_api prompt JSON (dict keyed by node "
                "id). The format is auto-detected per input and reported in the "
                "X-Workflow-Diff-Format-A / -B response headers. "
                "Red = removed, Green = added, Yellow = changed widgets, Blue = moved. "
                'mode is "all" (default; full graph) or "changed_only" (only nodes '
                "that actually changed). In changed_only mode, pure move/resize "
                "is hidden by default; set include_moved=true to include them. "
                'layout is "topo" (default; topological layered re-layout) or '
                '"preserve" (editor positions, only honoured when both inputs '
                "are UI format; everything else is rendered with topo)."
            ),
            "ui": "/workflow/diff/ui",
        }
    )


# ------------------------------------------------------- /workflow/diff/ui
async def diff_ui(request: web.Request) -> web.Response:
    """A tiny self-contained HTML page for pasting two workflows and previewing the diff."""
    return web.Response(text=_DIFF_UI_HTML, content_type="text/html")


# ------------------------------------------------------------- /is_ui
async def is_ui_endpoint(request: web.Request) -> web.Response:
    """POST a workflow JSON. Returns ``{"is_ui": true}`` when the input
    looks like the full UI workflow format (has ``nodes`` / ``links``)
    and ``{"is_ui": false}`` when it looks like the workflow_api prompt
    format (dict keyed by node id with ``class_type``). 400 if neither.
    """
    try:
        if request.content_length is not None and request.content_length > MAX_CONTENT_LENGTH:
            return web.json_response(
                {"error": f"Request too large. Max {MAX_CONTENT_LENGTH // (1024 * 1024)} MB"},
                status=413,
            )
        data = await request.json()
    except json.JSONDecodeError as e:
        return web.json_response({"error": f"Invalid JSON: {e}"}, status=400)

    fmt = detect_format(data)
    if fmt is None:
        return web.json_response(
            {
                "error": (
                    "Unrecognised workflow shape — expected UI format "
                    "(with 'nodes' and 'links' arrays) or API format "
                    "(dict of node descriptors with 'class_type')."
                )
            },
            status=400,
        )
    return web.json_response({"is_ui": fmt == "ui"})


# ------------------------------------------------------------------ utils
def _validate_workflow(data: Any, *, name: str) -> str | None:
    if not isinstance(data, dict):
        return f"'{name}' must be a JSON object"
    if "nodes" not in data or "links" not in data:
        return f"'{name}' must contain 'nodes' and 'links' (full workflow format, not API format)"
    if not isinstance(data.get("nodes"), list):
        return f"'{name}.nodes' must be a list"
    if not isinstance(data.get("links"), list):
        return f"'{name}.links' must be a list"
    return None


# ------------------------------------------------------------ HTML preview
_DIFF_UI_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Workflow Diff</title>
<style>
  body { font-family: system-ui, sans-serif; background:#1a1a1a; color:#eee; margin:0; padding:16px; }
  h1 { font-size: 18px; margin:0 0 12px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; height:200px; }
  textarea { width:100%; height:100%; background:#222; color:#eee; border:1px solid #444;
             border-radius:6px; padding:8px; font-family:monospace; font-size:11px; resize:vertical; }
  button { background:#22c55e; color:#000; border:0; padding:8px 16px; border-radius:6px;
           font-weight:600; cursor:pointer; margin:12px 0; }
  #out { width:100%; min-height:400px; background:#202020; border:1px solid #333;
         border-radius:6px; padding:8px; overflow:auto; }
  .err { color:#f87171; }
</style></head>
<body>
  <h1>Workflow Diff Preview</h1>
  <div class="grid">
    <textarea id="a" placeholder="Paste workflow A (the 'before')"></textarea>
    <textarea id="b" placeholder="Paste workflow B (the 'after')"></textarea>
  </div>
  <div style="margin:8px 0">
    <label><select id="mode">
      <option value="all">all (full graph)</option>
      <option value="changed_only">changed_only (only changed nodes)</option>
    </select></label>
    <label style="margin-left:12px"><select id="layout">
      <option value="topo">topo layout (default)</option>
      <option value="preserve">preserve editor positions</option>
    </select></label>
    <label style="margin-left:12px"><input type="checkbox" id="moved" />
      include moved (changed_only only)</label>
    <button id="go" style="margin-left:12px">Render diff</button>
  </div>
  <div id="msg"></div>
  <div id="out"></div>
<script>
document.getElementById('go').addEventListener('click', async () => {
  const msg = document.getElementById('msg');
  const out = document.getElementById('out');
  msg.textContent = ''; msg.className = '';
  out.innerHTML = '';
  let a, b;
  try { a = JSON.parse(document.getElementById('a').value); }
  catch (e) { msg.textContent = 'Workflow A: ' + e; msg.className='err'; return; }
  try { b = JSON.parse(document.getElementById('b').value); }
  catch (e) { msg.textContent = 'Workflow B: ' + e; msg.className='err'; return; }
  const mode = document.getElementById('mode').value;
  const layout = document.getElementById('layout').value;
  const include_moved = document.getElementById('moved').checked;
  const res = await fetch('/workflow/diff', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({workflow_a:a, workflow_b:b, mode, layout, include_moved})
  });
  if (!res.ok) {
    const text = await res.text();
    msg.textContent = 'Error ' + res.status + ': ' + text; msg.className='err'; return;
  }
  const svg = await res.text();
  out.innerHTML = svg;
});
</script>
</body></html>
"""


# ---------------------------------------------------------- route binding
# Bind handlers to ComfyUI's PromptServer only when it's actually
# available. Outside ComfyUI the import succeeds but no routes are
# registered, which lets the package be imported by tests / linters /
# tooling without needing a live ComfyUI process.
if PromptServer is not None:
    _routes = PromptServer.instance.routes
    _routes.post("/workflow/convert")(convert_workflow_endpoint)
    _routes.get("/workflow/convert")(converter_info)
    _routes.post("/workflow/diff")(diff_workflow_endpoint)
    _routes.get("/workflow/diff")(diff_info)
    _routes.get("/workflow/diff/ui")(diff_ui)
    _routes.post("/workflow/is_ui")(is_ui_endpoint)
    print(
        f"[workflow-diff v{__version__}] endpoints registered: "
        "/workflow/diff, /workflow/diff/ui, /workflow/is_ui, /workflow/convert"
    )


NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}
