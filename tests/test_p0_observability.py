"""P0 observability facts use the existing canonical event/trajectory stores."""

from __future__ import annotations

import asyncio
import json

import pytest


def test_observability_event_has_common_fields_and_trajectory_buffer(tmp_path, monkeypatch):
    from server import events

    store = events.EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr(events, "event_store", store)
    token = events.bind_event_context(
        session_id="session-p0",
        trace_id="trace-p0",
        turn_id="turn-p0",
    )
    buffer_token = events.bind_observability_events()
    capability_token = events.bind_event_capability("coding")
    try:
        recorded = events.append_observability_event(
            "capability.decision",
            task_id="task-p0",
            capability="coding",
            action="select_capability",
            status="selected",
            payload={"execution_mode": "goal"},
        )
        collected = events.current_observability_events()
    finally:
        events.reset_event_capability(capability_token)
        events.reset_observability_events(buffer_token)
        events.reset_event_context(token)

    assert recorded["topic"] == "capability.decision"
    payload = recorded["payload"]
    assert payload == {
        "execution_mode": "goal",
        "task_id": "task-p0",
        "trace_id": "trace-p0",
        "goal_run_id": None,
        "capability": "coding",
        "tool": None,
        "action": "select_capability",
        "status": "selected",
    }
    assert collected == [recorded]
    persisted = store.read_all(task_id="task-p0")
    assert persisted[0]["trace_id"] == "trace-p0"

    from server.trajectory import append_trajectory, build_trajectory, read_trajectories

    trajectory_path = tmp_path / "trajectory.jsonl"
    append_trajectory(
        build_trajectory(
            task_id="task-p0",
            objective="observe",
            outcome="completed",
            tool_calls=[],
            duration_ms=1,
            steps=collected,
            trace_id="trace-p0",
        ),
        path=trajectory_path,
    )
    trajectory = read_trajectories("task-p0", path=trajectory_path)[0]
    assert trajectory["steps"][0]["payload"]["capability"] == "coding"
    assert trajectory["steps"][0]["payload"]["status"] == "selected"


@pytest.mark.asyncio
async def test_coordinator_tool_events_have_common_fields(tmp_path, monkeypatch):
    from server import events
    from server.coordinator_master import MasterCoordinator

    store = events.EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr(events, "event_store", store)
    coordinator = MasterCoordinator.__new__(MasterCoordinator)
    coordinator._tool_timeout_s = None

    async def raw_tool(_name: str, _args: dict[str, object]) -> str:
        return "ok"

    coordinator._raw_handle_tool_call = raw_tool
    context_token = events.bind_event_context(
        session_id="session-tool",
        trace_id="trace-tool",
        turn_id="turn-tool",
    )
    task_token = events._task_id_ctx.set("task-tool")
    capability_token = events.bind_event_capability("research")
    try:
        assert (
            await coordinator._guarded_handle_tool_call("fetch_url", {"url": "https://x"}) == "ok"
        )
    finally:
        events.reset_event_capability(capability_token)
        events._task_id_ctx.reset(task_token)
        events.reset_event_context(context_token)

    observed = store.read_all(task_id="task-tool")
    assert [event["topic"] for event in observed] == ["tool.call", "tool.result"]
    assert all(
        event["payload"][key] == expected
        for event, expected in zip(observed, ["started", "completed"], strict=True)
        for key in ("status",)
    )
    assert all(event["payload"]["tool"] == "fetch_url" for event in observed)
    assert all(event["payload"]["capability"] == "research" for event in observed)


@pytest.mark.asyncio
async def test_approval_events_preserve_same_action_on_resume(tmp_path, monkeypatch):
    from server import events
    from server import user_control as uc

    store = events.EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr(events, "event_store", store)
    context_token = events.bind_event_context(
        session_id="session-approval",
        trace_id="trace-approval",
        turn_id="turn-approval",
    )
    capability_token = events.bind_event_capability("coding")
    control_tokens = uc.activate(mode="agent", require_approval=True, session_id="session-approval")
    try:
        waiting = asyncio.create_task(uc._wait_approval("coding_task_run", {"x": 1}))
        await asyncio.sleep(0)
        request_id = next(iter(uc._pending))
        assert uc.resolve_approval(request_id, True)
        assert await waiting is None
    finally:
        uc.deactivate(control_tokens)
        uc._pending.clear()
        events.reset_event_capability(capability_token)
        events.reset_event_context(context_token)

    observed = store.read_all(session_id="session-approval")
    lifecycle = [
        event for event in observed if event["topic"] in {"approval.suspended", "approval.resumed"}
    ]
    assert [event["topic"] for event in lifecycle] == [
        "approval.suspended",
        "approval.resumed",
    ]
    assert lifecycle[0]["payload"]["action"] == lifecycle[1]["payload"]["action"]
    assert all(event["payload"]["capability"] == "coding" for event in lifecycle)


def test_goal_run_retry_emits_replan_facts(tmp_path, monkeypatch):
    from server import events
    from server.goal_run import runner
    from server.goal_run.models import GoalRunState, TaskNode, TaskStatus

    store = events.EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr(events, "event_store", store)
    state = GoalRunState(goal_id="goal-p0", goal_text="retry")
    task = TaskNode(
        id="task-p0",
        title="retry task",
        instruction="do it",
        acceptance=["done"],
        depends_on=[],
        assignee="builtin",
        status=TaskStatus.ready,
        retries=0,
    )
    state.tasks[task.id] = task
    state.budget["max_retries_per_task"] = 2
    context_token = events.bind_event_context(
        session_id="session-p0",
        trace_id="trace-p0",
        turn_id="turn-p0",
    )
    capability_token = events.bind_event_capability("coding")
    try:
        runner._emit_runtime_event(
            state,
            str(tmp_path),
            "replan.started",
            task_id=task.id,
            action="retry",
            status="started",
            reason="bad",
            retry_attempt=1,
        )
        runner._emit_runtime_event(
            state,
            str(tmp_path),
            "replan.completed",
            task_id=task.id,
            action="retry",
            status="completed",
            retry_attempt=1,
        )
    finally:
        events.reset_event_capability(capability_token)
        events.reset_event_context(context_token)

    assert task.status == TaskStatus.ready
    observed = store.read_all(session_id="session-p0")
    assert [event["topic"] for event in observed] == [
        "replan.started",
        "replan.completed",
    ]
    assert all(event["payload"]["goal_run_id"] == "goal-p0" for event in observed)
    local_events = [
        json.loads(line)
        for line in (tmp_path / ".veya-project" / "goal-runs" / "goal-p0" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert [event["type"] for event in local_events] == [
        "replan.started",
        "replan.completed",
    ]


@pytest.mark.asyncio
async def test_react_tool_failure_emits_replan_before_next_tool_step(tmp_path, monkeypatch):
    from server import events
    from server.coordinator_master import _REPLAN_STATE_CTX, MasterCoordinator
    from server.tool_registry import ToolExecutionError

    store = events.EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr(events, "event_store", store)
    coordinator = MasterCoordinator.__new__(MasterCoordinator)
    coordinator._tool_timeout_s = None

    async def raw_tool(_name: str, args: dict[str, object]) -> str:
        if not args:
            raise ToolExecutionError("missing filepath")
        return "verified"

    coordinator._raw_handle_tool_call = raw_tool
    event_context = events.bind_event_context(
        session_id="session-replan",
        trace_id="trace-replan",
        turn_id="turn-replan",
    )
    task_token = events._task_id_ctx.set("task-replan")
    capability_token = events.bind_event_capability("knowledge")
    replan_token = _REPLAN_STATE_CTX.set({})
    try:
        with pytest.raises(ToolExecutionError):
            await coordinator._guarded_handle_tool_call("read_file_ast", {})
        assert (
            await coordinator._guarded_handle_tool_call("read_file_ast", {"filepath": "README.md"})
            == "verified"
        )
    finally:
        _REPLAN_STATE_CTX.reset(replan_token)
        events.reset_event_capability(capability_token)
        events._task_id_ctx.reset(task_token)
        events.reset_event_context(event_context)

    observed = store.read_all(task_id="task-replan")
    assert [event["topic"] for event in observed] == [
        "tool.call",
        "tool.result",
        "replan.started",
        "replan.completed",
        "tool.call",
        "tool.result",
    ]
    assert observed[2]["payload"]["failed_tool"] == "read_file_ast"
    assert observed[3]["payload"]["replacement_tool"] == "read_file_ast"
    assert observed[-1]["payload"]["status"] == "completed"
