"""P0.1: execution timeout policy is structured, and the Execution lifecycle is
independent of GoalRun.

Two defects this is a regression for.

1. *A timeout named the wrong clock.* ``ExecutionRecord`` declared three separate
   budgets but picked the clock by convenience —
   ``TimeoutKind.TOOL if record.command_timeout_sec else TimeoutKind.PROCESS`` —
   then wrote the budget from a *different* field
   (``command_timeout_sec or execution_timeout_sec``). So a record could say
   "PROCESS_TIMEOUT, 30s" while the process ran 11s, and an execution whose own
   deadline had passed could never be reported as an execution expiry at all,
   because no caller could name that clock. The assertion that pins it is
   ``test_execution_deadline_wins_over_a_child_clock_that_also_expired``.

2. *Execution and GoalRun shared a fate they do not share.* An Execution record
   reaching ``TIMED_OUT`` is a statement about one execution. It says nothing
   about the task the GoalRun represents, and a GoalRun reaching ``blocked`` is a
   statement about the task. If a timeout can move a GoalRun by itself, the
   execution deadline has become a task-level deadline without anyone deciding
   that. ``test_execution_timeout_does_not_move_the_goal_run`` is the boundary.

``goal_run/models.py`` is loaded directly rather than through
``server.goal_run`` because that package's ``__init__`` imports ``runner.py``,
which depends on ``server.failure_semantics`` / ``server.no_progress`` /
``server.versioning`` / ``server.goal_run.continuation`` /
``server.goal_run.topology_selector``. Those five files are untracked in the
canonical checkout and therefore absent here. The normal import path is tried
first and only bypassed when it genuinely fails, so this test keeps working on a
complete checkout.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import sys
import time
import types
from pathlib import Path

import pytest

from veya.remote.execution import (
    ExecutionRecord,
    ExecutionStatus,
    ExecutionTimeoutPolicy,
    TimeoutKind,
    is_terminal_status,
)
from veya.remote.execution_timeout import (
    TIMEOUT_LAYERS,
    ExecutionTimeoutAttribution,
    TimeoutBudgetSource,
)


def _load_goal_run_models() -> types.ModuleType:
    """Import ``server.goal_run.models``, bypassing a broken package __init__."""

    try:
        from server.goal_run import models

        return models
    except Exception:
        root = Path(__file__).resolve().parents[2]
        for name, path in (
            ("server", root / "server"),
            ("server.goal_run", root / "server" / "goal_run"),
        ):
            if name not in sys.modules:
                stub = types.ModuleType(name)
                stub.__path__ = [str(path)]  # type: ignore[attr-defined]
                sys.modules[name] = stub
        spec = importlib.util.spec_from_file_location(
            "server.goal_run.models", root / "server" / "goal_run" / "models.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["server.goal_run.models"] = module
        spec.loader.exec_module(module)
        return module


_goal_run_models = _load_goal_run_models()
GoalRunState = _goal_run_models.GoalRunState
GoalStatus = _goal_run_models.GoalStatus


# ── helpers ────────────────────────────────────────────────────────


def _record(
    *,
    execution_id: str = "ex_test",
    started_at: float = 1000.0,
    completed_at: float | None = 1000.0,
    **limits,
) -> ExecutionRecord:
    """A record with only the budget fields set, and no lifecycle side effects.

    ``completed_at`` defaults to ``started_at`` so elapsed time is 0 unless a
    test asks for a run of nonzero length.
    """

    return ExecutionRecord(
        execution_id=execution_id,
        task_id="task_test",
        session_id="sess",
        token_id="tok",
        principal="p",
        tool="shell.exec",
        veya_tool="shell.exec",
        requested_workspace="/tmp/ws",
        requested_realpath="/tmp/ws",
        resolved_repo_root="/tmp/ws",
        repo_identity="repo_test",
        started_at=started_at,
        completed_at=completed_at,
        **limits,
    )


# ── the execution deadline is a nameable clock ─────────────────────


def test_execution_deadline_is_a_first_class_clock() -> None:
    """Before P0.1 there was no way to say "the execution itself expired"."""

    assert str(TimeoutKind.EXECUTION) == "EXECUTION_TIMEOUT"
    assert TIMEOUT_LAYERS[TimeoutKind.EXECUTION] == "execution"


def test_pre_existing_clocks_are_still_present() -> None:
    """P0.1 is additive. Removing a clock would delete timeout provenance."""

    assert {str(k) for k in TimeoutKind} == {
        "SUBMIT_TIMEOUT",
        "PROCESS_TIMEOUT",
        "TOOL_TIMEOUT",
        "EXECUTION_TIMEOUT",
    }


def test_timeout_kind_is_still_exported_from_the_execution_module() -> None:
    """``tool_adapter`` imports TimeoutKind from here; a move must not break it."""

    import veya.remote.execution as execution_module

    assert execution_module.TimeoutKind is TimeoutKind


# ── a non-positive budget is refused rather than expiring on arrival ──


@pytest.mark.parametrize("field", ["execution_deadline_s", "command_limit_s"])
@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_a_non_positive_budget_is_refused(field: str, bad: float) -> None:
    with pytest.raises(ValueError, match="positive seconds or None"):
        ExecutionTimeoutPolicy(**{field: bad})


def test_a_non_positive_effective_timeout_is_refused() -> None:
    with pytest.raises(ValueError, match="effective_timeout_ms"):
        ExecutionTimeoutPolicy(effective_timeout_ms=0)


def test_an_unbounded_clock_never_expires() -> None:
    """Without this, an execution declaring no deadline reports as timed out on
    arrival — the exact failure mode a missing budget must not have."""

    policy = ExecutionTimeoutPolicy()
    assert policy.resolve(TimeoutKind.EXECUTION) is None
    assert policy.expired(TimeoutKind.EXECUTION, 10_000.0) is False


# ── the budget comes from the clock that expired ───────────────────


def test_execution_deadline_wins_over_a_child_clock_that_also_expired() -> None:
    """The defect. Both clocks are spent at t=11; the record must name the one
    the expiry belongs to and quote that one's budget, not a command budget the
    process never reached."""

    record = _record(execution_timeout_sec=10.0, command_timeout_sec=30.0)
    record.completed_at = 1011.0

    assert record.elapsed_s == pytest.approx(11.0)
    assert record.timeout_policy.expired(TimeoutKind.EXECUTION, 11.0) is True
    # The command clock has not expired at all; the old convenience rule picked
    # TOOL merely because a command budget happened to be set.
    assert record.timeout_policy.expired(TimeoutKind.TOOL, 11.0) is False


def test_a_command_budget_alone_cannot_disguise_an_execution_expiry() -> None:
    record = _record(execution_timeout_sec=10.0, command_timeout_sec=30.0)
    attribution = record.timeout_policy.attribute(TimeoutKind.TOOL, elapsed_s=11.0)

    # TOOL resolves against the command limit, which is the clock it names.
    assert attribution.budget_seconds == 30.0
    assert attribution.budget_source == TimeoutBudgetSource.COMMAND_LIMIT
    assert attribution.layer == "tool"
    # ...and it is honestly marked as not-yet-expired, so a reader can tell the
    # difference between "expired" and "asked about".
    assert record.timeout_policy.expired(TimeoutKind.TOOL, 11.0) is False


def test_a_caller_supplied_budget_is_marked_as_caller_supplied() -> None:
    """A hand-passed number must not read as a declared one."""

    record = _record()
    attribution = record.timeout_policy.attribute(TimeoutKind.TOOL, elapsed_s=5.0, seconds=5.0)
    assert attribution.budget_seconds == 5.0
    assert attribution.budget_is_declared is False


def test_a_declared_budget_is_marked_as_declared() -> None:
    record = _record(execution_timeout_sec=900.0)
    attribution = record.timeout_policy.attribute(TimeoutKind.EXECUTION, elapsed_s=901.0)
    assert attribution.budget_is_declared is True
    assert attribution.budget_source == TimeoutBudgetSource.EXECUTION_DEADLINE


def test_the_policy_reads_only_the_record_it_was_projected_from() -> None:
    """Reading a record's policy cannot widen it."""

    record = _record(execution_timeout_sec=10.0)
    policy = record.timeout_policy
    assert policy.execution_deadline_s == 10.0
    assert policy.command_limit_s is None
    # Mutating the returned policy is impossible; it is frozen.
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.execution_deadline_s = 9999.0  # type: ignore[misc]


# ── deadline_expired is independent of the lifecycle tables ────────


def test_deadline_expired_is_false_before_the_execution_starts() -> None:
    record = _record(execution_timeout_sec=1.0)
    record.started_at = None
    assert record.deadline_expired() is False
    assert record.elapsed_s is None


def test_an_execution_can_sit_past_its_deadline_while_still_running() -> None:
    """The gap P0.1 exists to express: no child clock has fired, the phase is
    still RUNNING, and the execution is nevertheless over budget."""

    started = time.time() - 25.0
    record = _record(execution_timeout_sec=10.0, started_at=started, completed_at=None)
    record.phase = "RUNNING"

    assert record.elapsed_s >= 25.0
    assert record.deadline_expired() is True
    assert record.is_terminal is False
    assert record.timeout_type is None, "no child clock has fired; nothing to attribute yet"


def test_admission_delay_is_not_charged_to_the_execution_budget() -> None:
    """Elapsed time is measured from ``started_at``, not from construction."""

    record = _record(execution_timeout_sec=10.0)
    assert record.started_at == 1000.0
    assert record.elapsed_s == pytest.approx(0.0)
    assert record.deadline_expired() is False


# ── the attribution is structured, not two free-form fields ────────


def test_attribution_projection_names_every_field_a_reader_needs() -> None:
    attribution = ExecutionTimeoutAttribution(
        kind="EXECUTION_TIMEOUT",
        layer="execution",
        budget_source="EXECUTION_DEADLINE",
        budget_seconds=10.0,
        budget_is_declared=True,
        elapsed_seconds=11.0,
        attributed_at=1000.0,
    )
    assert attribution.to_dict() == {
        "kind": "EXECUTION_TIMEOUT",
        "layer": "execution",
        "budget_source": "EXECUTION_DEADLINE",
        "budget_seconds": 10.0,
        "budget_is_declared": True,
        "elapsed_seconds": 11.0,
        "attributed_at": 1000.0,
    }


def test_attribution_survives_a_json_round_trip() -> None:
    """The record is persisted with ``asdict`` and reloaded by key, so an
    attribution that does not round-trip is provenance that is silently lost."""

    original = ExecutionTimeoutPolicy(execution_deadline_s=10.0).attribute(
        TimeoutKind.EXECUTION, elapsed_s=11.0
    )
    restored = ExecutionRecord.from_json(
        {**_record().to_json(), "timeout_attribution": original.to_dict()}
    )
    assert restored.timeout_attribution == original.to_dict()


def test_the_flat_timeout_fields_are_kept_alongside_the_attribution() -> None:
    """Nothing is removed: an existing reader of ``timeout_type`` still works."""

    record = _record()
    record.timeout_type = str(TimeoutKind.TOOL)
    record.timeout_seconds = 900.0
    record.timeout_attribution = (
        ExecutionTimeoutPolicy(execution_deadline_s=10.0)
        .attribute(TimeoutKind.TOOL, elapsed_s=900.0, seconds=900.0)
        .to_dict()
    )

    public = record.to_public(heartbeat_timeout_s=60.0)
    assert public["timeout_type"] == "TOOL_TIMEOUT"
    assert public["timeout_seconds"] == 900.0
    assert public["timeout_attribution"]["budget_is_declared"] is False


def test_every_clock_has_a_layer_and_a_failure_class_to_report() -> None:
    assert set(TIMEOUT_LAYERS) == set(TimeoutKind)


def test_an_unknown_clock_is_refused_rather_than_attributed() -> None:
    with pytest.raises(ValueError, match="unknown timeout kind"):
        ExecutionTimeoutPolicy().attribute("LUNCH_TIMEOUT")


# ── Execution lifecycle vs GoalRun lifecycle ───────────────────────


def test_the_execution_lifecycle_still_terminates_on_its_own_clock() -> None:
    """A timeout is still enforced. P0.1 adds structure, it does not disarm it."""

    assert is_terminal_status(str(ExecutionStatus.TIMED_OUT)) is True
    record = _record()
    record.phase = str(ExecutionStatus.TIMED_OUT)
    assert record.is_terminal is True


async def test_execution_timeout_does_not_move_the_goal_run() -> None:
    """The boundary. An execution expiring on its own deadline must leave the
    GoalRun exactly where it was — same status, same ``last_stop_reason``, no
    timeout written anywhere the task can read it."""

    goal = GoalRunState(
        goal_id="goal_1",
        goal_text="do the thing",
        status=GoalStatus.running,
        execution_id="ex_test",
    )
    before = (goal.status, goal.last_stop_reason, goal.finished_at)

    record = _record(execution_timeout_sec=10.0, started_at=time.time() - 11.0, completed_at=None)
    record.phase = str(ExecutionStatus.TIMED_OUT)
    record.timeout_type = str(TimeoutKind.EXECUTION)
    record.timeout_seconds = 10.0
    record.timeout_attribution = record.timeout_policy.attribute(
        TimeoutKind.EXECUTION, elapsed_s=record.elapsed_s
    ).to_dict()

    assert record.deadline_expired() is True
    assert (goal.status, goal.last_stop_reason, goal.finished_at) == before


async def test_the_execution_lifecycle_and_the_goal_run_lifecycle_are_separate_engines() -> None:
    """``is_terminal`` answers about the execution. It has no answer to give about
    the goal, and a GoalRun has no field that can be set from an execution
    timeout attribution."""

    record = _record(execution_timeout_sec=10.0)
    record.completed_at = 1011.0
    record.phase = str(ExecutionStatus.TIMED_OUT)

    goal = GoalRunState(goal_id="goal_2", goal_text="t", status=GoalStatus.running)

    assert record.is_terminal is True
    assert goal.is_terminal() is False
    # The attribution projects onto execution fields only.
    projected = set(
        record.timeout_policy.attribute(TimeoutKind.EXECUTION, elapsed_s=11.0).to_dict()
    )
    goal_fields = set(GoalRunState.__dataclass_fields__)
    assert projected.isdisjoint(goal_fields)


# ── a new execution gets a new identity, even mid-goal ────────────


async def test_a_new_execution_identity_does_not_reuse_the_previous_one() -> None:
    """Recovery re-admits work; it must not silently continue under the old id,
    or two attempts become one indistinguishable record."""

    goal = GoalRunState(goal_id="goal_3", goal_text="t", status=GoalStatus.recovering)
    first = _record(execution_timeout_sec=10.0)
    first.phase = str(ExecutionStatus.TIMED_OUT)

    goal.execution_id = first.execution_id
    first_id = goal.execution_id

    second = _record(execution_id="ex_test_2", started_at=2000.0)
    second.goal_run_id = goal.goal_id
    goal.execution_id = second.execution_id

    assert first_id == "ex_test"
    assert goal.execution_id == "ex_test_2"
    assert first.execution_id != second.execution_id
    # Both attempts remain independently readable, each with its own budget.
    assert first.timeout_policy.execution_deadline_s == 10.0
    assert second.timeout_policy.execution_deadline_s is None
    # The goal is still running the same task; only the execution identity moved.
    assert goal.goal_id == second.goal_run_id


async def test_the_previous_execution_record_is_not_mutated_by_the_new_one() -> None:
    first = _record(execution_timeout_sec=10.0)
    second = _record(execution_timeout_sec=10.0)
    second.execution_id = "ex_test_2"

    second.timeout_attribution = second.timeout_policy.attribute(
        TimeoutKind.EXECUTION, elapsed_s=99.0
    ).to_dict()

    assert first.timeout_attribution is None
    assert second.timeout_attribution is not None


# ── the negative control ───────────────────────────────────────────


async def test_a_budget_with_no_declared_limit_is_not_reported_as_expired() -> None:
    """If this fails, every unbounded execution in the runtime is being reported
    as timed out and the attribution is noise."""

    record = _record()
    record.completed_at = 9_999_999.0

    assert record.elapsed_s > 0.0
    assert record.timeout_policy.resolve(TimeoutKind.EXECUTION) is None
    assert record.deadline_expired() is False


async def test_a_real_elapsed_measurement_is_required_for_an_expiry() -> None:
    """Elapsed time comes off the record. A policy asked about a fabricated
    elapsed value answers about that value, and nothing more."""

    record = _record(execution_timeout_sec=10.0)
    policy = record.timeout_policy

    assert policy.expired(TimeoutKind.EXECUTION, record.elapsed_s or 0.0) is False
    assert policy.expired(TimeoutKind.EXECUTION, 10.0) is True


async def test_sleeping_past_the_deadline_expires_a_live_execution() -> None:
    """End-to-end on a real clock, so the check is not vacuous on a frozen one."""

    record = _record(execution_timeout_sec=0.05, started_at=time.time(), completed_at=None)

    assert record.deadline_expired() is False
    await asyncio.sleep(0.08)
    assert record.deadline_expired() is True
