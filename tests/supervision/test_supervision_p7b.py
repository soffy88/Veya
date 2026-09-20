"""P7B: durable execution handles + restart reconciliation (no silent re-dispatch)."""

from __future__ import annotations

import types
from pathlib import Path

from veya.supervision import MissionStore, SupervisionRouter
from veya.supervision.loop import STATE_COMPLETED, STATE_INTERRUPTED, STATE_RUNNING, MissionLoop

_SUCCESS = {"done", "accepted", "success", "succeeded", "pass", "passed", "completed"}


def _state(summary: str = "ok") -> types.SimpleNamespace:
    node = types.SimpleNamespace(
        title="t", assignee="dsh", status="completed", acceptance=["ok"], verify_summary="passed",
        artifacts=[], evidence=[], retries=0, block_reason=None, unfinished_work=[],
    )
    return types.SimpleNamespace(
        goal_id="gr-1", status="executed", tasks={"t": node}, final_summary=summary, unfinished_work=[]
    )


class CountingRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, mission):
        self.calls += 1
        return _state()


def _loop(tmp_path: Path, runner, *, restart: bool = False) -> MissionLoop:
    """A fresh process == a fresh store + loop over the same project root."""
    store = MissionStore(tmp_path)
    return MissionLoop(store=store, router=SupervisionRouter(store), runner=runner)


def _mission(tmp_path: Path, *, executor: str, mode: str = "internal") -> str:
    store = MissionStore(tmp_path)
    mission = _real_mission(executor, mode, str(tmp_path))
    store.save(mission)
    return mission.mission_id


def _real_mission(executor: str, mode: str, workspace: str):
    from veya.supervision.models import Mission, MissionPolicies, SupervisionMode

    return Mission(
        mission_id="mission-x",
        goal="g",
        supervision_mode=SupervisionMode(mode),
        workspace=workspace,
        policies=MissionPolicies(execution_policy={"assignee_hint": executor}),
    )


def test_job_handle_durable(tmp_path):
    mission_id = _mission(tmp_path, executor="dsh")
    runner = CountingRunner()
    import asyncio

    asyncio.run(_loop(tmp_path, runner).step(mission_id))
    # a *different* process reads the handle back from disk
    handle = MissionStore(tmp_path).execution_for(mission_id, 0)
    assert handle is not None
    assert handle["state"] == STATE_COMPLETED
    assert handle["execution_id"] == f"{mission_id}:0"
    assert handle["executor"] == "dsh"
    assert handle["started_at"] and handle["attempt"] == 1


def test_restart_during_execution_marks_interrupted(tmp_path):
    for executor in ("dsh", "hicode"):
        root = tmp_path / executor
        root.mkdir()
        mission_id = _mission(root, executor=executor)
        # simulate: handle written, executor started, process died before the report
        MissionStore(root).append_execution(
            mission_id,
            {
                "execution_id": f"{mission_id}:0",
                "iteration": 0,
                "executor": executor,
                "started_at": 1.0,
                "state": STATE_RUNNING,
                "attempt": 1,
            },
        )
        MissionStore(root).append_event(mission_id, "EXECUTOR_STARTED", {"iteration": 0})

        runner = CountingRunner()
        snapshot = __import__("asyncio").run(_loop(root, runner, restart=True).step(mission_id))
        assert runner.calls == 0, f"{executor}: must not silently re-dispatch"
        assert "INTERRUPTED" in str(snapshot), f"{executor}: restart must be reconciled"
        report = MissionStore(root).get_report(mission_id, 0)
        assert report is not None and str(report.status) == STATE_INTERRUPTED
        assert report.proposed_next_action == "retry"
        assert report.failures and report.failures[0]["kind"] == "execution_interrupted"
        handle = MissionStore(root).execution_for(mission_id, 0)
        assert handle["state"] == STATE_INTERRUPTED


def test_same_execution_id_after_restart(tmp_path):
    mission_id = _mission(tmp_path, executor="dsh")
    handle = MissionStore(tmp_path).execution_for(mission_id, 0)
    assert handle is None
    MissionStore(tmp_path).append_execution(
        mission_id,
        {"execution_id": f"{mission_id}:0", "iteration": 0, "executor": "dsh", "state": STATE_RUNNING},
    )
    # after "restart" the deterministic id is unchanged (no new random identity)
    assert MissionStore(tmp_path).execution_for(mission_id, 0)["execution_id"] == f"{mission_id}:0"


def test_no_duplicate_side_effects_on_completed_iteration(tmp_path):
    mission_id = _mission(tmp_path, executor="dsh")
    runner = CountingRunner()
    import asyncio

    asyncio.run(_loop(tmp_path, runner).step(mission_id))
    assert runner.calls == 1
    # restart: the persisted report is authoritative, the executor is not called again
    asyncio.run(_loop(tmp_path, runner, restart=True).step(mission_id))
    assert runner.calls == 1, "a completed iteration must never be re-executed"


def test_lost_job_false_success_zero(tmp_path):
    mission_id = _mission(tmp_path, executor="dsh")
    MissionStore(tmp_path).append_execution(
        mission_id,
        {"execution_id": f"{mission_id}:0", "iteration": 0, "executor": "dsh", "state": STATE_RUNNING},
    )
    import asyncio

    asyncio.run(_loop(tmp_path, CountingRunner()).step(mission_id))
    report = MissionStore(tmp_path).get_report(mission_id, 0)
    assert str(report.status).lower() not in _SUCCESS
    assert report.proposed_next_action != "accept"
