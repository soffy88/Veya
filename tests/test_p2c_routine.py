"""P2-C: Routine registry + trigger dispatch — trigger-only, idempotent, durable.

Routines never execute, never finalize, and never create (parallel) GoalRuns.
One trigger identity starts at most once per routine, including across
restarts: consumption lives in the existing GoalRunState (taskgraph.json).
"""

from __future__ import annotations

import pytest

from runtime.execution.models import (
    ROUTINE_ACCEPTANCE_AUTHORITY,
    ROUTINE_EXECUTION_AUTHORITY,
    SECOND_ACCEPTANCE_AUTHORITY,
    SECOND_EXECUTION_AUTHORITY,
    assert_routine_execution_authority_zero,
)
from server.goal_run.models import GoalRunState, RoutineSpec
from server.goal_run.routine_dispatch import HANDOFF_VIA, dispatch_trigger
from server.goal_run.routine_registry import RoutineRegistry, validate_routine_spec
from server.goal_run.store import load_goal_run, save_goal_run


def _registry(tmp_path) -> RoutineRegistry:
    return RoutineRegistry(tmp_path / "routines.json")


def _skill_routine(routine_id: str = "morning.brief", version: int = 2) -> RoutineSpec:
    return RoutineSpec(
        routine_id=routine_id,
        trigger_topic="schedule.trigger",
        objective="deliver morning brief",
        version=version,
        target_skill_id="brief.render",
        evidence_requirements=["brief-sha"],
    )


def _playbook_routine(routine_id: str = "weekly.report") -> RoutineSpec:
    return RoutineSpec(
        routine_id=routine_id,
        trigger_topic="event.arrive",
        objective="publish weekly report",
        target_playbook_id="publish.report",
    )


def test_routine_register_get_list(tmp_path):
    reg = _registry(tmp_path)
    reg.register(_skill_routine())
    reg.register(_playbook_routine())
    assert reg.get("morning.brief", version=2) is not None
    assert reg.get("morning.brief", version=999) is None
    assert reg.get("missing") is None
    assert {r.routine_id for r in reg.list()} == {"morning.brief", "weekly.report"}
    assert validate_routine_spec(_skill_routine()) == []
    assert any(
        "trigger_topic" in e
        for e in validate_routine_spec(
            RoutineSpec(routine_id="x", trigger_topic="nope", objective="o", target_skill_id="s")
        )
    )
    with pytest.raises(ValueError, match="exactly one target"):
        reg.register(
            RoutineSpec(
                routine_id="two",
                trigger_topic="event.arrive",
                objective="o",
                target_skill_id="s",
                target_playbook_id="p",
            )
        )
    with pytest.raises(ValueError, match="exactly one target"):
        reg.register(RoutineSpec(routine_id="none", trigger_topic="event.arrive", objective="o"))


def test_routine_trigger_dispatch(tmp_path):
    reg = _registry(tmp_path)
    reg.register(_skill_routine())
    state = GoalRunState(goal_id="goal-r", goal_text="routine dispatch")
    handoffs = dispatch_trigger(
        state,
        reg.lookup("schedule.trigger"),
        topic="schedule.trigger",
        trigger_id="tick-1",
    )
    assert len(handoffs) == 1
    handoff = handoffs[0]
    assert handoff["routine_id"] == "morning.brief"
    assert handoff["version"] == 2
    assert handoff["trigger_identity"] == "schedule.trigger:tick-1"
    assert handoff["goal_run_id"] == "goal-r"
    assert handoff["via"] == list(HANDOFF_VIA) == ["veya-bot", "master-agent", "goal-run"]
    entry = state.routine_states["morning.brief"]
    assert entry.started_count == 1
    assert entry.last_trigger_ref == "tick-1"
    assert entry.canonical_goal_run_id == "goal-r"
    with pytest.raises(ValueError, match="unknown trigger topic"):
        dispatch_trigger(state, [], topic="cron.rewrite", trigger_id="x")


def test_routine_skill_target(tmp_path):
    reg = _registry(tmp_path)
    reg.register(_skill_routine())
    state = GoalRunState(goal_id="goal-s", goal_text="skill target")
    (handoff,) = dispatch_trigger(
        state, reg.lookup("schedule.trigger"), topic="schedule.trigger", trigger_id="t1"
    )
    assert handoff["target"] == {"skill_id": "brief.render"}
    assert handoff["evidence_requirements"] == ["brief-sha"]


def test_routine_playbook_target(tmp_path):
    reg = _registry(tmp_path)
    reg.register(_playbook_routine())
    state = GoalRunState(goal_id="goal-p", goal_text="playbook target")
    (handoff,) = dispatch_trigger(
        state, reg.lookup("event.arrive"), topic="event.arrive", trigger_id="e1"
    )
    assert handoff["target"] == {"playbook_id": "publish.report"}


def test_same_goalrun(tmp_path):
    reg = _registry(tmp_path)
    reg.register(_skill_routine())
    state = GoalRunState(goal_id="goal-a", goal_text="first run")
    dispatch_trigger(
        state, reg.lookup("schedule.trigger"), topic="schedule.trigger", trigger_id="t1"
    )
    other = GoalRunState(goal_id="goal-b", goal_text="other run")
    other.routine_states.update(state.routine_states)
    with pytest.raises(ValueError, match="belongs to GoalRun"):
        dispatch_trigger(
            other,
            reg.lookup("schedule.trigger"),
            topic="schedule.trigger",
            trigger_id="t2",
        )


def test_routine_restart_resume_and_trigger_state_preserved(tmp_path):
    reg = _registry(tmp_path)
    reg.register(_skill_routine())
    state = GoalRunState(goal_id="goal-re", goal_text="restart")
    dispatch_trigger(
        state, reg.lookup("schedule.trigger"), topic="schedule.trigger", trigger_id="t1"
    )
    save_goal_run(state, str(tmp_path))
    # New supervisor process, SAME GoalRun file.
    resumed = load_goal_run(str(tmp_path), "goal-re")
    assert resumed is not None
    entry = resumed.routine_states["morning.brief"]
    assert entry.version == 2
    assert entry.consumed_trigger_ids == ["schedule.trigger:t1"]
    assert entry.started_count == 1
    assert entry.last_trigger_ref == "t1"
    assert entry.canonical_goal_run_id == "goal-re"
    # Consumed trigger never restarts; a fresh trigger dispatches.
    assert (
        dispatch_trigger(
            resumed,
            reg.lookup("schedule.trigger"),
            topic="schedule.trigger",
            trigger_id="t1",
        )
        == []
    )
    fresh = dispatch_trigger(
        resumed, reg.lookup("schedule.trigger"), topic="schedule.trigger", trigger_id="t2"
    )
    assert len(fresh) == 1
    assert resumed.routine_states["morning.brief"].started_count == 2
    save_goal_run(resumed, str(tmp_path))
    final = load_goal_run(str(tmp_path), "goal-re")
    assert final is not None
    assert final.routine_states["morning.brief"].consumed_trigger_ids == [
        "schedule.trigger:t1",
        "schedule.trigger:t2",
    ]


def test_duplicate_routine_start_is_zero(tmp_path):
    reg = _registry(tmp_path)
    reg.register(_skill_routine())
    state = GoalRunState(goal_id="goal-dupe", goal_text="duplicates")
    started = 0
    for _ in range(5):
        started += len(
            dispatch_trigger(
                state,
                reg.lookup("schedule.trigger"),
                topic="schedule.trigger",
                trigger_id="same-tick",
            )
        )
    assert started == 1
    assert state.routine_states["morning.brief"].started_count == 1


def test_disabled_routine_never_dispatches(tmp_path):
    reg = _registry(tmp_path)
    reg.register(_skill_routine())
    assert reg.set_enabled("morning.brief", False) is True
    assert reg.lookup("schedule.trigger") == []
    assert reg.list() == []
    assert len(reg.list(include_disabled=True)) == 1


def test_routine_authority_zeros():
    assert ROUTINE_EXECUTION_AUTHORITY == 0
    assert ROUTINE_ACCEPTANCE_AUTHORITY == 0
    assert SECOND_EXECUTION_AUTHORITY == 0
    assert SECOND_ACCEPTANCE_AUTHORITY == 0
    assert_routine_execution_authority_zero()
