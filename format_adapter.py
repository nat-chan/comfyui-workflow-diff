"""
Detect a workflow's serialization format and coerce it to UI format so the
diff / renderer (which speak UI format) can consume either.

Two formats are recognised:

* **UI format** — the "full" workflow JSON that ComfyUI's *Save* button
  produces. Top-level keys include ``nodes`` (list) and ``links`` (list).
  Each node carries ``id``, ``type``, ``pos``, ``size``, ``inputs``,
  ``outputs``, ``widgets_values`` etc.

* **API format** — the prompt JSON that ComfyUI's *Save (API)* button
  produces and that ``/prompt`` accepts. Top-level is a flat dict whose
  values are node descriptors carrying ``class_type`` and ``inputs``.

``detect_format`` returns ``"ui"``, ``"api"`` or ``None``.
``coerce_to_ui`` returns a UI-format workflow regardless of which side
the user fed in; API workflows are run through
``workflow_api_adapter.api_to_ui_workflow``.
"""

from __future__ import annotations

from typing import Any, Literal

try:
    from .widget_names import promoted_input_names
    from .workflow_api_adapter import api_to_ui_workflow
except ImportError:  # standalone execution / tests
    from widget_names import promoted_input_names  # type: ignore
    from workflow_api_adapter import api_to_ui_workflow  # type: ignore


Format = Literal["ui", "api"]


def detect_format(data: Any) -> Format | None:
    """Best-effort format detection.

    Returns ``"ui"`` when ``data`` looks like a full workflow (has list
    ``nodes`` and ``links``), ``"api"`` when it looks like a prompt
    (top-level dict whose values are ``{"class_type": ..., "inputs": ...}``),
    and ``None`` when neither pattern matches.
    """
    if not isinstance(data, dict):
        return None
    nodes = data.get("nodes")
    links = data.get("links")
    if isinstance(nodes, list) and isinstance(links, list):
        return "ui"
    # Need at least one entry that *looks* like an API node.
    for v in data.values():
        if isinstance(v, dict) and "class_type" in v:
            return "api"
    return None


def coerce_to_ui(
    data: Any,
    *,
    format_hint: Format | None = None,
    layout_hints: dict[str, tuple[float, float]] | None = None,
    field_name: str = "workflow",
) -> tuple[dict[str, Any], Format]:
    """Return ``(ui_workflow, detected_format)``.

    ``format_hint`` lets a direct caller skip auto-detection. The HTTP
    endpoint never passes this — it always auto-detects via
    ``prepare_diff_inputs``. The kwarg is kept here for programmatic use
    only.

    For API inputs, ``layout_hints`` is forwarded to the adapter so both
    sides of a diff can share the same coordinate system.
    """
    fmt = format_hint or detect_format(data)
    if fmt == "ui":
        return data, "ui"
    if fmt == "api":
        if not isinstance(data, dict):
            raise ValueError(f"{field_name}: API workflow must be a JSON object")
        return api_to_ui_workflow(data, layout_hints=layout_hints), "api"
    raise ValueError(
        f"{field_name}: unrecognised workflow shape — expected UI format "
        "(with 'nodes' and 'links' arrays) or API format (dict of node "
        "descriptors with 'class_type')."
    )


def _backfill_widget_names(
    workflow_a: dict[str, Any],
    workflow_b: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Cross-type widget-name backfill across two UI-format workflows.

    Builds a ``class_type -> widget_names`` registry from every node that
    already carries ``_widget_names`` (typically the API-adapter side)
    and stamps the matching names onto same-typed nodes that don't yet
    have any. Both diff and render benefit — without this, a B-only
    custom node (UI side) would have no name source outside ComfyUI.

    The workflow dicts are shallow-copied and so are the nodes we
    actually mutate; the caller's input is never modified in place.
    """
    registry: dict[str, list[str]] = {}
    for src in (workflow_a, workflow_b):
        for n in src.get("nodes") or []:
            ntype = n.get("type")
            if not isinstance(ntype, str) or ntype in registry:
                continue
            cached = n.get("_widget_names")
            if isinstance(cached, list) and cached:
                registry[ntype] = list(cached)

    # The API adapter trims promoted-to-input widgets out of
    # ``_widget_names`` (since their values aren't in widgets_values
    # either). But when the OTHER side has the same widget as a normal
    # widget — its widgets_values *does* include the slot, and we need
    # a canonical name for it to reconcile correctly. ComfyUI's UI JSON
    # preserves the original widget name in ``inputs[].widget.name``
    # whenever a widget was converted to an input, so harvesting those
    # gives us names the trimmed registry misses. We prepend them on
    # the heuristic that promoted widgets tend to come early in
    # INPUT_TYPES (true for KSampler's ``seed`` — the canonical case).
    promoted_by_type: dict[str, list[str]] = {}
    seen_per_type: dict[str, set[str]] = {}
    for src in (workflow_a, workflow_b):
        for n in src.get("nodes") or []:
            ntype = n.get("type")
            if not isinstance(ntype, str):
                continue
            already = seen_per_type.setdefault(ntype, set())
            for name in promoted_input_names(n):
                if name in already:
                    continue
                already.add(name)
                promoted_by_type.setdefault(ntype, []).append(name)

    for ntype, promoted_names in promoted_by_type.items():
        existing = registry.get(ntype, [])
        existing_set = set(existing)
        missing = [n for n in promoted_names if n not in existing_set]
        if missing:
            registry[ntype] = missing + existing

    def _apply(wf: dict[str, Any]) -> dict[str, Any]:
        new_nodes: list[dict[str, Any]] = []
        changed = False
        for n in wf.get("nodes") or []:
            ntype = n.get("type")
            existing = n.get("_widget_names")
            if isinstance(existing, list) and existing:
                new_nodes.append(n)
                continue
            if isinstance(ntype, str) and ntype in registry:
                new_n = dict(n)
                new_n["_widget_names"] = list(registry[ntype])
                new_nodes.append(new_n)
                changed = True
            else:
                new_nodes.append(n)
        if not changed:
            return wf
        return {**wf, "nodes": new_nodes}

    return _apply(workflow_a), _apply(workflow_b)


def prepare_diff_inputs(
    raw_a: Any,
    raw_b: Any,
) -> tuple[dict[str, Any], dict[str, Any], Format, Format]:
    """End-to-end coercion for the diff endpoint.

    Returns ``(ui_a, ui_b, fmt_a, fmt_b)``. Format is always auto-detected
    from the input shape.

    No layout alignment is performed here. The caller is responsible for
    deciding the layout policy — typically: ``preserve`` is only valid
    when both inputs are UI format (the workflow JSON the user authored
    in the editor); anything else is rendered with the topo layout, which
    recomputes positions from the union graph and naturally aligns
    common nodes between A and B.
    """
    fmt_a = detect_format(raw_a)
    fmt_b = detect_format(raw_b)
    if fmt_a is None:
        raise ValueError(
            "workflow_a: unrecognised workflow shape — expected UI format "
            "(with 'nodes' and 'links' arrays) or API format (dict of node "
            "descriptors with 'class_type')."
        )
    if fmt_b is None:
        raise ValueError(
            "workflow_b: unrecognised workflow shape — expected UI format "
            "(with 'nodes' and 'links' arrays) or API format (dict of node "
            "descriptors with 'class_type')."
        )

    ui_a, _ = coerce_to_ui(raw_a, format_hint=fmt_a, field_name="workflow_a")
    ui_b, _ = coerce_to_ui(raw_b, format_hint=fmt_b, field_name="workflow_b")
    ui_a, ui_b = _backfill_widget_names(ui_a, ui_b)
    return ui_a, ui_b, fmt_a, fmt_b
