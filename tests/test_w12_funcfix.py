"""funcfix 2026-10-14 — W12 supervised runs actually start and their binding closes.

Live failure: ``supervise_task`` only recorded a GoalRun shell (running, no
tasks, never started) and nothing executed it; the binding reported "stuck"
forever and never closed, also when a run ended blocked/failed.

All offline: no network, no model.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.conftest import ROOT  # noqa: F401 — path bootstrap


@pytest.fixture
def supervisor_on(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("VEYA_SUPERVISOR", "1")
    monkeypatch.setenv("VEYA_SUPERVISOR_BINDING_PATH", str(tmp_path / "supervisor_bindings.json"))
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routine_registry.json"))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    monkeypatch.setenv("VEYA_WRITE_ROOT", str(tmp_path / "work"))
    from server.supervisor import binding

    yield binding


def _binding(tmp_path, goal_id, **kw):
    from server.supervisor.binding import SupervisorBinding

    values = dict(
        binding_id=f"b_{goal_id}",
        goal_run_id=goal_id,
        routine_id=f"r_{goal_id}",
        owner_id="bot_test",
        bot_id="bot_test",
        objective="统计 README 行数",
        project_root=str(tmp_path),
        report_every_minutes=5,
        stall_after_minutes=20,
        max_repairs=3,
        last_activity_at=time.time(),
    )
    values.update(kw)
    return SupervisorBinding(**values)


def test_supervise_task_dispatches_the_run_it_records(supervisor_on, monkeypatch, tmp_path):
    from server import supervision_tools

    calls = []
    monkeypatch.setattr(
        supervision_tools, "_dispatch_supervised_run", lambda **kw: calls.append(kw) or True
    )
    out = supervision_tools.supervise_task(
        project_root="", objective="统计 README 行数", bot_id="bot_x", dispatch=True
    )
    assert out["dispatched"] is True
    assert len(calls) == 1
    call = calls[0]
    assert call["goal_run_id"] == out["goal_run_id"]
    assert call["binding_id"] == out["binding"]["binding_id"]
    assert call["bot_id"] == "bot_x"
    # Never the process cwd: one absolute root shared by shell, binding and run.
    assert Path(call["project_root"]).is_absolute()
    assert out["binding"]["project_root"] == call["project_root"]
    from server.goal_run.store import load_goal_run

    assert load_goal_run(call["project_root"], out["goal_run_id"]) is not None


def test_supervise_task_dispatch_can_be_disabled(supervisor_on, monkeypatch, tmp_path):
    from server import supervision_tools

    monkeypatch.setattr(
        supervision_tools,
        "_dispatch_supervised_run",
        lambda **kw: pytest.fail("must not dispatch"),
    )
    out = supervision_tools.supervise_task(
        project_root=str(tmp_path), objective="x", bot_id="bot_x", dispatch=False
    )
    assert out["dispatched"] is False


@pytest.mark.asyncio
async def test_runner_plans_a_preregistered_shell_under_its_own_id(monkeypatch, tmp_path):
    """The dispatched run must plan under the id the binding already holds."""
    from server.goal_run import runner
    from server.goal_run.models import GoalRunState, GoalStatus
    from server.goal_run.store import save_goal_run

    shell = GoalRunState(
        goal_id="goal_shell_1", goal_text="x", status=GoalStatus.running, bot_id="bot_x"
    )
    save_goal_run(shell, str(tmp_path))

    seen = {}

    class _Stop(Exception):
        pass

    async def fake_g1_plan(**kw):
        seen.update(kw)
        raise _Stop

    monkeypatch.setattr(runner, "g1_plan", fake_g1_plan)
    with pytest.raises(_Stop):
        await runner.project_run_goal(
            project_root=str(tmp_path),
            goal="x",
            mode="act_eager",
            resume_goal_id="goal_shell_1",
        )
    assert seen["goal_id"] == "goal_shell_1"
    assert seen["bot_id"] == "bot_x"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["blocked", "failed", "partial_completed"])
async def test_binding_closes_when_run_ends_without_passing(supervisor_on, tmp_path, status):
    from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus
    from server.goal_run.store import save_goal_run
    from server.supervisor.handoff import supervisor_handoff

    goal_id = f"goal_end_{status}"
    state = GoalRunState(goal_id=goal_id, goal_text="x", status=GoalStatus(status), bot_id="bot_test")
    state.tasks["t1"] = TaskNode(
        id="t1", title="t", instruction="do", acceptance=["ok"], depends_on=[],
        assignee="builtin", status=TaskStatus.blocked,
    )
    state.last_stop_reason = "Missing required evidence"
    save_goal_run(state, str(tmp_path))
    supervisor_on.save_binding(_binding(tmp_path, goal_id))

    await supervisor_handoff(
        binding=supervisor_on.get_binding(f"b_{goal_id}"),
        goal_run_id=goal_id,
        project_root=str(tmp_path),
    )
    closed = supervisor_on.get_binding(f"b_{goal_id}")
    assert closed.status == "closed"
    assert closed.closed_reason == status


@pytest.mark.asyncio
async def test_binding_stays_open_while_run_progresses(supervisor_on, tmp_path):
    from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus
    from server.goal_run.store import save_goal_run
    from server.supervisor.handoff import supervisor_handoff

    state = GoalRunState(goal_id="goal_live", goal_text="x", status=GoalStatus.running, bot_id="bot_test")
    for tid, st in (("t1", TaskStatus.completed), ("t2", TaskStatus.running)):
        state.tasks[tid] = TaskNode(
            id=tid, title=tid, instruction="do", acceptance=["ok"], depends_on=[],
            assignee="builtin", status=st,
        )
    save_goal_run(state, str(tmp_path))
    supervisor_on.save_binding(_binding(tmp_path, "goal_live"))
    await supervisor_handoff(
        binding=supervisor_on.get_binding("b_goal_live"),
        goal_run_id="goal_live",
        project_root=str(tmp_path),
    )
    assert supervisor_on.get_binding("b_goal_live").status == "open"


@pytest.mark.asyncio
async def test_binding_gives_up_after_repairs_exhausted(supervisor_on, tmp_path):
    from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus
    from server.goal_run.store import save_goal_run
    from server.supervisor.handoff import supervisor_handoff

    state = GoalRunState(goal_id="goal_stuck", goal_text="x", status=GoalStatus.running, bot_id="bot_test")
    state.tasks["t1"] = TaskNode(
        id="t1", title="t", instruction="do", acceptance=["ok"], depends_on=[],
        assignee="builtin", status=TaskStatus.running,
    )
    save_goal_run(state, str(tmp_path))
    b = _binding(
        tmp_path, "goal_stuck", nudges_sent=3, max_repairs=3,
        last_activity_at=time.time() - 3600, stall_after_minutes=1,
    )
    # Same fingerprint as what the run shows now -> no fresh activity.
    b.last_report_fingerprint = "running|0|1|0|None"
    supervisor_on.save_binding(b)
    await supervisor_handoff(
        binding=supervisor_on.get_binding("b_goal_stuck"),
        goal_run_id="goal_stuck",
        project_root=str(tmp_path),
    )
    closed = supervisor_on.get_binding("b_goal_stuck")
    assert closed.status == "closed" and closed.closed_reason == "gave_up"


@pytest.mark.asyncio
async def test_supervised_run_executes_through_the_bot_task_path(monkeypatch, tmp_path):
    """The run must go through the product task path (it has an executor).

    A bare ROUTINE request ended the live run partial in 1s with
    "no executor requested; a leaf must name an admitted executor".
    """
    from server import supervision_tools
    from server.routes import product as product_routes

    seen = {}

    async def fake_open(**kw):
        seen["open"] = kw
        return "sess_1", "task_1"

    async def fake_run(**kw):
        seen["run"] = kw
        return "ok"

    monkeypatch.setattr(product_routes, "_open_task_projection", fake_open)
    monkeypatch.setattr(product_routes, "_run_product_task", fake_run)
    out = await supervision_tools._run_supervised_goal(
        project_root=str(tmp_path),
        objective="统计 README 行数",
        goal_run_id="goal_x",
        bot_id="bot_x",
        binding_id="sup_x",
        user={"user_id": "u1"},
        provider="p",
        model="m",
    )
    assert out == "ok"
    run = seen["run"]
    assert run["resume_goal_id"] == "goal_x"
    assert run["bot_id"] == "bot_x"
    assert run["project_root"] == str(tmp_path)
    assert run["task_id"] == "task_1" and run["session_id"] == "sess_1"
    assert (run["provider"], run["model"]) == ("p", "m")
    assert run["user"]["user_id"] == "u1"
    assert seen["open"]["workspace_id"] == str(tmp_path)


def test_supervise_tool_entry_runs_on_the_event_loop():
    import inspect

    from server import supervision_tools

    entry = next(t for t in supervision_tools._TOOLS if t[0] == "supervise_task")
    assert inspect.iscoroutinefunction(entry[3])
