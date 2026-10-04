"""A public tool description must not send the model to a tool that is gone.

``assemble_code_context`` and ``evolve_solution`` both told the model to prefer
``hicode_run``. That tool is not registered — Hicode was retired from the
executor plane and the registration went with it — so following the guidance
produced a call to a name the model cannot resolve, while the description read
as an authoritative routing rule.

Descriptions are the model's only view of the surface, so a stale name here is a
runtime inconsistency, not a documentation nit: the registry and the prompt
disagreed and only the model could see it.

``omodul.AgentLoop`` was the same class of problem one level down: an internal
module path named in a description the model reads.
"""

from __future__ import annotations

from server.tool_registry import master_tools


def _registered() -> set[str]:
    try:
        return set(master_tools.list_tools())
    except Exception:  # pragma: no cover - registry shape guard
        return set(getattr(master_tools, "_tools", {}))


def _descriptions() -> dict[str, str]:
    """Public tool name -> description, read from the live registry.

    Read from ``master_tools.describe`` rather than parsed out of the source:
    an earlier version of this file scanned the source with a regex, matched
    zero descriptions, and both negative tests below passed vacuously.
    """

    return {name: master_tools.describe(name) or "" for name in master_tools.list_tools()}


def test_no_description_points_at_the_retired_hicode_tool():
    registered = _registered()
    offenders = {name: text for name, text in _descriptions().items() if "hicode_run" in text}
    assert not offenders, (
        "these descriptions route the model to hicode_run, which is not registered: "
        f"{sorted(offenders)}; registered coding entry point is coding_task_run"
    )
    # Stated rather than assumed: the replacement really is available.
    assert "coding_task_run" in registered


def test_no_description_names_an_internal_module_path():
    internal = ("omodul.", "oprim.", "oskill.", "engine_runner", "run_engine", "_harness_argv")
    offenders = {
        name: [sym for sym in internal if sym in text]
        for name, text in _descriptions().items()
        if any(sym in text for sym in internal)
    }
    assert not offenders, f"internal implementation symbols in public descriptions: {offenders}"


def test_no_eval_expectation_points_at_an_unregistered_tool():
    """An eval case expecting a tool that does not exist can never score.

    ``code_modification_needs_delegation`` expected ``hicode_run``. That tool
    went with the executor retirement, so the case was unreachable — it looked
    like coverage of the delegation rule and was not.
    """

    import pathlib
    import re

    from server.tool_registry import master_tools

    registered = set(master_tools.list_tools())
    source = (pathlib.Path(__file__).resolve().parents[2] / "server" / "agent_eval.py").read_text(
        encoding="utf-8"
    )
    expected = re.findall(r'expected=\{"tool": "([\w.]+)"', source)
    assert expected, "the extraction found no expectations at all"
    unknown = [name for name in expected if name not in registered]
    assert not unknown, f"eval cases expect unregistered tools and can never pass: {unknown}"


def test_the_tool_group_table_has_no_retired_hicode_entries():
    """The grouping table feeds the isolated subtask surface; keep it honest."""

    from server.tool_registry import _TOOL_GROUPS

    stale = [name for name in _TOOL_GROUPS if "hicode" in name]
    assert not stale, f"retired executor still in _TOOL_GROUPS: {stale}"


def test_the_sweep_actually_inspects_real_descriptions():
    # A negative assertion with no positive case proves nothing: make sure the
    # extraction really reads descriptions, including the two that were repaired.
    described = _descriptions()
    assert len(described) > 100, len(described)
    assert all(described.values()), "every registered tool must expose a description"
    assert "evolve_solution" in described
    assert "assemble_code_context" in described
    assert "coding_task_run" in described["evolve_solution"]
    assert "coding_task_run" in described["assemble_code_context"]
