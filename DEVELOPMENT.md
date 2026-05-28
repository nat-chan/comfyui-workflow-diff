# Development notes

Contributor-facing only — end users don't need this.

## Tests

```bash
pytest                          # runs the test suite in tests/, no ComfyUI required
python tests/render_gallery.py  # renders a browsable SVG gallery (see below)
```

Tests cover format auto-detection, all four (A-format, B-format)
combinations, the diff record shape, SVG output structure, the
changed-only filter, and the preserve-vs-topo layout rules.

The fixture under `tests/fixtures/toy/` is a hand-crafted public-safe
workflow — a four-node pipeline
(`LoadImage → ImageScale → ImageInvert → SaveImage`) that only uses
ComfyUI **core** nodes, requires no checkpoints, and never touches a
GPU. Both the UI workflow JSON and the corresponding API prompt JSON
are included for each side of the diff so all four format combinations
are exercised end-to-end. See `tests/fixtures/toy/README.md` for the
expected diff shape.

## SVG gallery for visual inspection

`tests/render_gallery.py` POSTs the toy fixture pairs to a running
ComfyUI's `/workflow/diff` endpoint (so widget names come from the live
node registry, matching what production HTTP clients actually see) and
writes the SVGs plus an `index.html` for browsing. Useful for spotting
visual regressions that the assertion-based tests would miss.

```bash
# ComfyUI must be running with this custom node loaded.
python tests/render_gallery.py
xdg-open tests/_gallery/index.html   # or: open ... (macOS) / start ... (Windows)
# point at a non-default URL:
COMFYUI_URL=http://other-host:8188 python tests/render_gallery.py
```

The gallery groups outputs by **modes** (`all` / `changed_only` /
`changed_only + include_moved`), **layouts** (`topo` / `preserve`), and
**format combinations** (UI×UI, API×API, UI×API, API×UI). To extend
it, add a `GalleryEntry` to the `ENTRIES` list in the script. The
output dir is gitignored. The hero image at `docs/preview.svg` is
synced from the `layout-topo` entry on every run.
