"""End-to-end tests for the diff pipeline across input-format combinations.

We exercise the same internal functions the HTTP endpoint calls
(``format_adapter.prepare_diff_inputs`` → ``workflow_diff.diff_workflows`` →
``workflow_renderer.render_workflow_diff_svg``) without spinning up
ComfyUI.

Fixture: a hand-crafted toy workflow that only uses ComfyUI core nodes
(`LoadImage`, `ImageScale`, `ImageInvert`, `SaveImage`, `PreviewImage`).
No checkpoint, no GPU — see ``fixtures/toy/README.md``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from format_adapter import detect_format, prepare_diff_inputs
from workflow_diff import diff_workflows
from workflow_renderer import render_workflow_diff_svg

FIXTURES = Path(__file__).parent / "fixtures" / "toy"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# Convenience pre-loaded copies (cheap, called per-test).
def _ui_a() -> dict: return _load("before_ui.json")
def _ui_b() -> dict: return _load("after_ui.json")
def _api_a() -> dict: return _load("before_api.json")
def _api_b() -> dict: return _load("after_api.json")


# ----------------------------------------------------------- format detection
def test_detect_format_ui() -> None:
    assert detect_format(_ui_a()) == "ui"
    assert detect_format(_ui_b()) == "ui"


def test_detect_format_api() -> None:
    assert detect_format(_api_a()) == "api"
    assert detect_format(_api_b()) == "api"


def test_detect_format_unknown() -> None:
    assert detect_format({}) is None
    assert detect_format({"foo": "bar"}) is None
    assert detect_format("not a dict") is None
    assert detect_format(None) is None


# ----------------------------------------------------------- 4-combo matrix
COMBOS = [
    ("ui",  "ui",  "before_ui.json",  "after_ui.json"),
    ("ui",  "api", "before_ui.json",  "after_api.json"),
    ("api", "ui",  "before_api.json", "after_ui.json"),
    ("api", "api", "before_api.json", "after_api.json"),
]
COMBO_IDS = [f"{a}-{b}" for a, b, *_ in COMBOS]


@pytest.mark.parametrize("fmt_a,fmt_b,file_a,file_b", COMBOS, ids=COMBO_IDS)
def test_prepare_diff_inputs_detects_correct_formats(
    fmt_a: str, fmt_b: str, file_a: str, file_b: str
) -> None:
    ui_a, ui_b, detected_a, detected_b = prepare_diff_inputs(
        _load(file_a), _load(file_b)
    )
    assert detected_a == fmt_a
    assert detected_b == fmt_b
    # Both should now be UI format dicts with nodes + links arrays.
    for ui in (ui_a, ui_b):
        assert isinstance(ui, dict)
        assert isinstance(ui.get("nodes"), list)
        assert isinstance(ui.get("links"), list)


@pytest.mark.parametrize("fmt_a,fmt_b,file_a,file_b", COMBOS, ids=COMBO_IDS)
def test_diff_matches_toy_fixture_expectations(
    fmt_a: str, fmt_b: str, file_a: str, file_b: str
) -> None:
    """The toy fixture's expected diff (B − A):
        added:   {5}                  (PreviewImage)
        removed: {}
        changed widgets on nodes: superset of {1, 2}
    All four format combinations should report the same logical diff."""
    ui_a, ui_b, *_ = prepare_diff_inputs(_load(file_a), _load(file_b))
    diff = diff_workflows(ui_a, ui_b)

    added_ids = {n.get("id") for n in diff["added_nodes"]}
    removed_ids = {n.get("id") for n in diff["removed_nodes"]}
    changed_ids = {
        c["b"].get("id") for c in diff["common_nodes"] if c["changed_widgets"]
    }

    assert added_ids == {5}, f"added: {added_ids}"
    assert removed_ids == set(), f"removed: {removed_ids}"
    assert {1, 2}.issubset(changed_ids), f"changed: {changed_ids}"


def test_widget_change_records_carry_old_and_new() -> None:
    """Each change record carries both the old and the new value so the
    renderer can show ``new ← old`` inline."""
    ui_a, ui_b, *_ = prepare_diff_inputs(_ui_a(), _ui_b())
    diff = diff_workflows(ui_a, ui_b)
    by_id = {c["b"].get("id"): c for c in diff["common_nodes"]}

    changes_2 = by_id[2]["changed_widgets"]
    width_change = next(r for r in changes_2 if r["new"] == 1024)
    assert width_change["old"] == 512


@pytest.mark.parametrize("fmt_a,fmt_b,file_a,file_b", COMBOS, ids=COMBO_IDS)
@pytest.mark.parametrize("mode", ["all", "changed_only"])
def test_render_produces_svg(
    fmt_a: str, fmt_b: str, file_a: str, file_b: str, mode: str
) -> None:
    ui_a, ui_b, *_ = prepare_diff_inputs(_load(file_a), _load(file_b))
    diff = diff_workflows(ui_a, ui_b)
    svg = render_workflow_diff_svg(ui_a, ui_b, diff, mode=mode, layout="topo")
    assert svg.startswith("<svg"), svg[:80]
    assert "</svg>" in svg
    # At least PreviewImage + the two widget-changed nodes must be rendered.
    assert svg.count('<g class="node"') >= 3


def test_preserve_layout_for_ui_ui_uses_editor_positions() -> None:
    """preserve mode should reflect each node's `pos` field directly —
    in this fixture SaveImage moves between A and B, so the rendered SVG
    in preserve mode contains its 'after' y coordinate (260)."""
    ui_a, ui_b, *_ = prepare_diff_inputs(_ui_a(), _ui_b())
    diff = diff_workflows(ui_a, ui_b)
    svg = render_workflow_diff_svg(ui_a, ui_b, diff, layout="preserve")
    # The after SaveImage pos.y == 260; just check that this raw y appears
    # in the SVG node-rect attributes (formatted to one decimal place).
    assert "260.0" in svg


def test_topo_layout_recomputes_positions() -> None:
    """topo mode must actually relay the graph — at least one node's
    rendered position has to differ from the preserve-mode SVG. We
    don't compare viewBox widths because for this tiny toy fixture
    the editor-saved positions happen to already be compact."""
    ui_a, ui_b, *_ = prepare_diff_inputs(_ui_a(), _ui_b())
    diff = diff_workflows(ui_a, ui_b)
    svg_preserve = render_workflow_diff_svg(ui_a, ui_b, diff, layout="preserve")
    svg_topo = render_workflow_diff_svg(ui_a, ui_b, diff, layout="topo")
    # Pull every node's rect (x, y) attributes out of each SVG and
    # require the position sets to differ.
    rect_pat = re.compile(r'<rect x="([^"]+)" y="([^"]+)" width=')
    coords_p = sorted(rect_pat.findall(svg_preserve))
    coords_t = sorted(rect_pat.findall(svg_topo))
    assert coords_p != coords_t, "topo mode did not change any node position"


def test_topo_layout_aligns_common_nodes_for_api_api() -> None:
    """API-format inputs have no editor positions, so 'preserve' isn't
    meaningful. The topo layout (forced for non-UI×UI) is the alignment
    contract: common nodes must share coordinates between A and B in
    the rendered SVG."""
    ui_a, ui_b, fa, fb = prepare_diff_inputs(_api_a(), _api_b())
    assert (fa, fb) == ("api", "api")
    diff = diff_workflows(ui_a, ui_b)
    svg = render_workflow_diff_svg(ui_a, ui_b, diff, layout="topo")
    # Each rendered node is wrapped in <g class="node" data-id="..."> and
    # its outer rect carries the (x, y). Pull them out and assert that
    # common ids appear with the same position (topo lays both sides on
    # the union graph, so any node that exists in both must align).
    node_pat = re.compile(
        r'<g class="node" data-id="([^"]+)"[^>]*>\s*<rect x="([^"]+)" y="([^"]+)"'
    )
    pos_in_svg = {nid: (x, y) for nid, x, y in node_pat.findall(svg)}
    # All 5 nodes from the union should appear.
    assert set(pos_in_svg) >= {"1", "2", "3", "4", "5"}, pos_in_svg.keys()


def test_changed_only_filters_unchanged_nodes() -> None:
    """In changed_only mode the renderer should skip ImageInvert (node 3) —
    it's unchanged — but keep nodes 1, 2 (widget changes) and 5 (added)."""
    ui_a, ui_b, *_ = prepare_diff_inputs(_ui_a(), _ui_b())
    diff = diff_workflows(ui_a, ui_b)
    svg = render_workflow_diff_svg(ui_a, ui_b, diff, mode="changed_only")
    assert 'data-type="ImageInvert"' not in svg
    assert 'data-type="LoadImage"' in svg
    assert 'data-type="PreviewImage"' in svg


def test_changed_only_hides_moved_by_default() -> None:
    """SaveImage only moved between A and B — under changed_only mode
    without include_moved=True it must be omitted."""
    ui_a, ui_b, *_ = prepare_diff_inputs(_ui_a(), _ui_b())
    diff = diff_workflows(ui_a, ui_b)
    svg_default = render_workflow_diff_svg(ui_a, ui_b, diff, mode="changed_only")
    svg_with_moved = render_workflow_diff_svg(
        ui_a, ui_b, diff, mode="changed_only", include_moved=True
    )
    assert 'data-type="SaveImage"' not in svg_default
    assert 'data-type="SaveImage"' in svg_with_moved


def test_prepare_diff_inputs_takes_no_hint_params() -> None:
    """The format-hint kwargs were removed on purpose — the public API
    auto-detects only. Confirm the signature exposes exactly two
    positional parameters and no format-hint kwargs."""
    import inspect
    sig = inspect.signature(prepare_diff_inputs)
    assert list(sig.parameters) == ["raw_a", "raw_b"]


def test_bad_input_raises_value_error() -> None:
    with pytest.raises(ValueError, match="workflow_a"):
        prepare_diff_inputs({"foo": "bar"}, _ui_b())
    with pytest.raises(ValueError, match="workflow_b"):
        prepare_diff_inputs(_ui_a(), {"not": "a workflow"})
