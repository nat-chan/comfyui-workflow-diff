"""Render a curated set of SVG diffs from the toy fixture into
``tests/_gallery/`` along with an ``index.html`` for browser viewing.

The script POSTs each fixture pair to a running ComfyUI's
``/workflow/diff`` endpoint, so widget names come from the live node
registry (``NODE_CLASS_MAPPINGS``) — the gallery matches what production
HTTP clients actually see. The in-process renderer is not used.

Usage::

    # ComfyUI must be running and serving this custom node.
    python tests/render_gallery.py
    # override the default URL if ComfyUI listens elsewhere:
    COMFYUI_URL=http://other-host:8188 python tests/render_gallery.py

Open the result with ``xdg-open tests/_gallery/index.html`` (or the
platform equivalent). The output dir is gitignored.

The gallery is a human aid for regression-spotting; the tests in
``test_format_combos.py`` are the authoritative correctness check and
do **not** require a running ComfyUI.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

FIXTURES = HERE / "fixtures" / "toy"
OUT_DIR = HERE / "_gallery"
# The README's hero image is committed to docs/preview.svg and kept in
# sync from the same source as the gallery's layout-topo entry.
README_PREVIEW = ROOT / "docs" / "preview.svg"
README_PREVIEW_SOURCE_SLUG = "layout-topo"

# Where this custom node's HTTP endpoint lives. Override with the
# ``COMFYUI_URL`` env var if your ComfyUI listens elsewhere.
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://localhost:8188").rstrip("/")
DIFF_ENDPOINT = f"{COMFYUI_URL}/workflow/diff"


@dataclass(frozen=True)
class GalleryEntry:
    slug: str
    title: str
    section: str
    description: str
    file_a: str
    file_b: str
    mode: str = "all"
    layout: str = "topo"
    include_moved: bool = False


# Curated set — every combination that demonstrates a distinct rendering
# decision. Adding new cases here is the right way to expand the gallery.
ENTRIES: list[GalleryEntry] = [
    # --- modes ------------------------------------------------------------
    GalleryEntry(
        slug="mode-all",
        title="mode=all (default)",
        section="Modes",
        description=(
            "Full graph including untouched nodes. Use for context: shows "
            "where the changes sit relative to the rest of the workflow."
        ),
        file_a="before_ui.json",
        file_b="after_ui.json",
    ),
    GalleryEntry(
        slug="mode-changed-only",
        title="mode=changed_only",
        section="Modes",
        description=(
            "Only nodes that participate in a change (added / removed / "
            "widget-changed). Moved-only nodes are hidden by default — "
            "ImageInvert and SaveImage disappear here."
        ),
        file_a="before_ui.json",
        file_b="after_ui.json",
        mode="changed_only",
    ),
    GalleryEntry(
        slug="mode-changed-only-with-moved",
        title="mode=changed_only + include_moved",
        section="Modes",
        description=(
            "Changed-only filter, but moved-only nodes are kept too — "
            "useful when you also care about layout shifts."
        ),
        file_a="before_ui.json",
        file_b="after_ui.json",
        mode="changed_only",
        include_moved=True,
    ),
    # --- layouts ----------------------------------------------------------
    GalleryEntry(
        slug="layout-topo",
        title="layout=topo (default)",
        section="Layouts",
        description=(
            "Topological layered re-layout — depth from a root determines "
            "the column. Editor-saved positions are ignored. Sizes are "
            "clamped to the natural slot+widget footprint."
        ),
        file_a="before_ui.json",
        file_b="after_ui.json",
    ),
    GalleryEntry(
        slug="layout-preserve",
        title="layout=preserve",
        section="Layouts",
        description=(
            "Editor positions and sizes are kept verbatim. Reflects what "
            "you'd see in the ComfyUI editor; image-preview nodes that "
            "embed a large preview will dominate the canvas."
        ),
        file_a="before_ui.json",
        file_b="after_ui.json",
        layout="preserve",
    ),
    # --- format combinations ---------------------------------------------
    GalleryEntry(
        slug="format-ui-ui",
        title="UI × UI",
        section="Format combinations",
        description=(
            "Both inputs are full UI workflow JSON. Widget names are not "
            "resolved here (we're running outside ComfyUI) so changed "
            "rows show just the values."
        ),
        file_a="before_ui.json",
        file_b="after_ui.json",
    ),
    GalleryEntry(
        slug="format-api-api",
        title="API × API",
        section="Format combinations",
        description=(
            "Both inputs are workflow_api prompt JSON. The adapter "
            "synthesizes a layered layout shared between the two sides; "
            "API inputs carry their own widget names, so changed rows "
            "show ``name: NEW ← OLD``."
        ),
        file_a="before_api.json",
        file_b="after_api.json",
    ),
    GalleryEntry(
        slug="format-ui-api",
        title="UI × API (mixed → forced topo)",
        section="Format combinations",
        description=(
            "Mixed input formats — editor positions and synthesized "
            "positions can't share a canvas meaningfully, so the topo "
            "layout is mandatory regardless of the layout request."
        ),
        file_a="before_ui.json",
        file_b="after_api.json",
    ),
    GalleryEntry(
        slug="format-api-ui",
        title="API × UI (mixed → forced topo)",
        section="Format combinations",
        description=(
            "Symmetric counterpart of the UI × API case. The detected "
            "format is reported in the X-Workflow-Diff-Format-* response "
            "headers at the HTTP endpoint level."
        ),
        file_a="before_api.json",
        file_b="after_ui.json",
    ),
]


class _ComfyUIUnreachable(RuntimeError):
    """The configured ComfyUI URL didn't respond to our request."""


def _render(entry: GalleryEntry) -> str:
    raw_a = json.loads((FIXTURES / entry.file_a).read_text())
    raw_b = json.loads((FIXTURES / entry.file_b).read_text())
    body = json.dumps(
        {
            "workflow_a": raw_a,
            "workflow_b": raw_b,
            "mode": entry.mode,
            "layout": entry.layout,
            "include_moved": entry.include_moved,
        }
    ).encode()
    req = urllib.request.Request(
        DIFF_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{DIFF_ENDPOINT} returned HTTP {e.code}: {body_text[:200]}"
        ) from e
    except urllib.error.URLError as e:
        raise _ComfyUIUnreachable(
            f"Could not reach {DIFF_ENDPOINT} ({e.reason}). "
            "Start ComfyUI (with this custom node loaded), or set "
            "COMFYUI_URL=http://host:port to point elsewhere."
        ) from e


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>workflow-diff SVG gallery</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ background:#141414; color:#e4e4e4; font-family: system-ui, -apple-system, sans-serif;
          margin:0; padding:24px 32px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  h2 {{ font-size:14px; margin:32px 0 12px; color:#9ad8ff; text-transform:uppercase;
        letter-spacing:0.08em; border-bottom:1px solid #333; padding-bottom:6px; }}
  .subtitle {{ color:#888; margin:0 0 28px; font-size:13px; }}
  .grid {{ display:grid; grid-template-columns: 1fr; gap:18px; }}
  .card {{ background:#1d1d1d; border:1px solid #2c2c2c; border-radius:10px;
           padding:14px 16px; }}
  .card header {{ display:flex; align-items:baseline; gap:12px; margin-bottom:8px; }}
  .card .title {{ font-size:15px; font-weight:600; color:#fff; }}
  .card .slug {{ font-size:11px; color:#7aa; font-family: ui-monospace, monospace; }}
  .card .desc {{ font-size:12px; line-height:1.55; color:#bbb; margin:0 0 12px; max-width:920px; }}
  .frame {{ background:#000; border-radius:6px; overflow:auto; max-height:680px; padding:6px; }}
  .frame svg {{ display:block; max-width:100%; height:auto; }}
  footer {{ margin-top:42px; color:#666; font-size:11px; }}
  a {{ color:#9ad8ff; }}
</style>
</head>
<body>
  <h1>workflow-diff — rendered SVG gallery</h1>
  <p class="subtitle">
    Generated by <code>tests/render_gallery.py</code> from
    <code>tests/fixtures/toy/</code> via the live <code>/workflow/diff</code>
    HTTP endpoint, so widget names come from ComfyUI's node registry.
    Regenerate after any rendering change to spot regressions visually.
    The test suite in <code>tests/test_format_combos.py</code> is the
    authoritative correctness check; this page is a human aid.
  </p>
  {sections}
  <footer>
    Source fixture: 4-node <code>LoadImage → ImageScale → ImageInvert → SaveImage</code>
    pipeline (ComfyUI core nodes, no GPU, no checkpoints). See
    <code>tests/fixtures/toy/README.md</code>.
  </footer>
</body>
</html>
"""


def _build_html(by_section: dict[str, list[tuple[GalleryEntry, str]]]) -> str:
    section_blocks = []
    for section_title, items in by_section.items():
        cards = []
        for entry, svg_filename in items:
            cards.append(
                f'<div class="card">'
                f'<header><span class="title">{escape(entry.title)}</span>'
                f'<span class="slug">{escape(entry.slug)}.svg</span></header>'
                f'<p class="desc">{escape(entry.description)}</p>'
                f'<div class="frame"><object type="image/svg+xml" '
                f'data="{escape(svg_filename)}"></object></div>'
                f'</div>'
            )
        section_blocks.append(
            f'<h2>{escape(section_title)}</h2>'
            f'<div class="grid">{"".join(cards)}</div>'
        )
    return _HTML_TEMPLATE.format(sections="\n".join(section_blocks))


def main() -> None:
    print(f"rendering via {DIFF_ENDPOINT}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Wipe stale renders so removed entries don't linger.
    for stale in OUT_DIR.glob("*.svg"):
        stale.unlink()

    by_section: dict[str, list[tuple[GalleryEntry, str]]] = {}
    try:
        for entry in ENTRIES:
            svg = _render(entry)
            filename = f"{entry.slug}.svg"
            (OUT_DIR / filename).write_text(svg)
            by_section.setdefault(entry.section, []).append((entry, filename))
            print(f"  wrote {filename} ({len(svg):,} bytes)")
            if entry.slug == README_PREVIEW_SOURCE_SLUG:
                README_PREVIEW.parent.mkdir(parents=True, exist_ok=True)
                README_PREVIEW.write_text(svg)
                print(f"  synced README preview → {README_PREVIEW.relative_to(ROOT)}")
    except _ComfyUIUnreachable as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(2)

    index_path = OUT_DIR / "index.html"
    index_path.write_text(_build_html(by_section))
    print(f"\nOpen {index_path}")


if __name__ == "__main__":
    main()
