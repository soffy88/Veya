"""Tests for W16 Project Brain: Patrol runbook and independent verification (Group B)."""

import time
from pathlib import Path
from unittest.mock import patch
import pytest

from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus
from server.goal_run.store import save_goal_run
from server.notification_center import global_notifier
from server.project_brain.brief import BriefRule, ProjectBrief, save_brief
from server.project_brain.patrol import check_disk_space, run_project_patrol
from server.project_brain.tasks import (
    TASK_STATUS_DONE,
    TASK_STATUS_FAILED,
    TASK_STATUS_RUNNING,
    ProjectTask,
    get_task,
    upsert_task,
)
from server.supervisor.binding import SupervisorBinding, save_binding


@pytest.fixture
def brain_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("VEYA_PROJECT_BRAIN", "1")
    monkeypatch.setenv("VEYA_SUPERVISOR", "1")
    monkeypatch.setenv("VEYA_SUPERVISOR_BINDING_PATH", str(tmp_path / "supervisor_bindings.json"))
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routine_registry.json"))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    monkeypatch.setenv("VEYA_NOTIFICATION_STORE_PATH", str(tmp_path / "notifications.json"))
    yield


@pytest.mark.asyncio
async def test_patrol_flag_disabled(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("VEYA_PROJECT_BRAIN", "0")
    binding = SupervisorBinding(
        binding_id="b1",
        goal_run_id="g1",
        routine_id="r1",
        owner_id="user_1",
        bot_id="bot_1",
        objective="test",
        project_root=str(tmp_path),
    )
    res = await run_project_patrol(binding=binding, goal_run_id="g1", project_root=str(tmp_path))
    assert res["verdict"] == "skipped"
    assert res["reason"] == "flag_disabled"


@pytest.mark.asyncio
async def test_patrol_disk_space_low(brain_env, tmp_path: Path):
    binding = SupervisorBinding(
        binding_id="b1",
        goal_run_id="g1",
        routine_id="r1",
        owner_id="user_1",
        bot_id="bot_1",
        objective="test",
        project_root=str(tmp_path),
    )
    notifications = []
    with patch.object(global_notifier, "push", side_effect=lambda **kwargs: notifications.append(kwargs)):
        with patch("shutil.disk_usage", return_value=type("Usage", (), {"free": 500 * 1024 * 1024})()):
            res = await run_project_patrol(binding=binding, goal_run_id="g1", project_root=str(tmp_path))
            assert res["verdict"] == "paused"
            assert res["reason"] == "disk_space_low"
            assert len(notifications) == 1
            assert notifications[0]["type"] == "WARNING"
            assert "磁盘空间不足" in notifications[0]["title"]


@pytest.mark.asyncio
async def test_patrol_independent_verification_usable(brain_env, tmp_path: Path):
    project_root = str(tmp_path)
    goal_id = "goal_test_usable"
    task_id = "ptask_test_1"

    # Create and save ProjectTask
    ptask = ProjectTask(
        task_id=task_id,
        objective="实现用户权限模块",
        goal_run_id=goal_id,
        binding_id="bind_1",
        routine_id="rout_1",
        status=TASK_STATUS_RUNNING,
    )
    upsert_task(project_root, ptask)

    # Create and save GoalRun with concrete, successful execute_result
    gstate = GoalRunState(goal_id=goal_id, goal_text="实现用户权限模块", bot_id="bot_1")
    gstate.tasks["task_a"] = TaskNode(
        id="task_a",
        title="编写权限模型",
        instruction="编写模型代码",
        acceptance=[],
        depends_on=[],
        assignee="bot_1",
        status=TaskStatus.completed,
        execute_result="Successfully generated auth model and schema in auth/models.py",
    )
    gstate.tasks["task_b"] = TaskNode(
        id="task_b",
        title="编写权限测试",
        instruction="编写单元测试",
        acceptance=[],
        depends_on=[],
        assignee="bot_1",
        status=TaskStatus.completed,
        execute_result="Ran 5 tests in test_auth.py: 5 passed, 0 failed.",
    )
    gstate.status = GoalStatus.completed
    save_goal_run(gstate, project_root)

    binding = SupervisorBinding(
        binding_id="bind_1",
        goal_run_id=goal_id,
        routine_id="rout_1",
        owner_id="user_alice",
        bot_id="bot_1",
        objective="实现用户权限模块",
        project_root=project_root,
    )
    save_binding(binding)

    notifications = []
    with patch.object(global_notifier, "push", side_effect=lambda **kwargs: notifications.append(kwargs)):
        res = await run_project_patrol(binding=binding, goal_run_id=goal_id, project_root=project_root)
        assert res["verdict"] == "done"
        assert res["evidence"]["usable"] is True
        assert "独立核验通过" in res["evidence"]["details"]

        # Task should be marked done
        updated_task = get_task(project_root, task_id)
        assert updated_task is not None
        assert updated_task.status == TASK_STATUS_DONE
        assert updated_task.verifier["usable"] is True

        # Supervisor binding should be closed
        assert binding.status == "closed"

        # Notification pushed
        assert any(n["type"] == "SUCCESS" and "任务完成" in n["title"] for n in notifications)

        # Run a second time to verify conclusion is sent only once (deduplication)
        notifications.clear()
        res2 = await run_project_patrol(binding=binding, goal_run_id=goal_id, project_root=project_root)
        # Second conclusion notification must NOT be pushed
        assert not any(n["type"] == "SUCCESS" and "任务完成" in n["title"] for n in notifications)


@pytest.mark.asyncio
async def test_patrol_independent_verification_unusable(brain_env, tmp_path: Path):
    project_root = str(tmp_path)
    goal_id = "goal_test_unusable"
    task_id = "ptask_test_2"

    ptask = ProjectTask(
        task_id=task_id,
        objective="实现搜索功能",
        goal_run_id=goal_id,
        binding_id="bind_2",
        routine_id="rout_2",
        status=TASK_STATUS_RUNNING,
    )
    upsert_task(project_root, ptask)

    # GoalRun tasks marked completed, but worker produced error or empty result
    gstate = GoalRunState(goal_id=goal_id, goal_text="实现搜索功能", bot_id="bot_1")
    gstate.tasks["task_x"] = TaskNode(
        id="task_x",
        title="搜索实现",
        instruction="代码编写",
        acceptance=[],
        depends_on=[],
        assignee="bot_1",
        status=TaskStatus.completed,
        execute_result="Error: failed to connect to elasticsearch service",
    )
    gstate.status = GoalStatus.running
    save_goal_run(gstate, project_root)

    binding = SupervisorBinding(
        binding_id="bind_2",
        goal_run_id=goal_id,
        routine_id="rout_2",
        owner_id="user_alice",
        bot_id="bot_1",
        objective="实现搜索功能",
        project_root=project_root,
    )

    res = await run_project_patrol(binding=binding, goal_run_id=goal_id, project_root=project_root)
    assert res["verdict"] != "done"
    assert res["evidence"]["usable"] is False
    assert "核验未通过" in res["evidence"]["details"]


@pytest.mark.asyncio
async def test_patrol_closing_actions_approval(brain_env, tmp_path: Path):
    project_root = str(tmp_path)
    goal_id = "goal_closing"
    task_id = "ptask_closing"

    # Set up brief with closing action rule
    brief = ProjectBrief(
        goal_scope="发布新版本",
        rules=[BriefRule(when="all tests pass", do="deploy to production", approval="ask")],
    )
    save_brief(project_root, brief)

    ptask = ProjectTask(
        task_id=task_id,
        objective="完成发布流程",
        goal_run_id=goal_id,
        binding_id="bind_close",
        status=TASK_STATUS_RUNNING,
    )
    upsert_task(project_root, ptask)

    gstate = GoalRunState(goal_id=goal_id, goal_text="完成发布流程", bot_id="bot_1")
    gstate.tasks["t1"] = TaskNode(
        id="t1",
        title="验证通过",
        instruction="测试",
        acceptance=[],
        depends_on=[],
        assignee="bot_1",
        status=TaskStatus.completed,
        execute_result="All tests passed successfully",
    )
    gstate.status = GoalStatus.completed
    save_goal_run(gstate, project_root)

    binding = SupervisorBinding(
        binding_id="bind_close",
        goal_run_id=goal_id,
        routine_id="rout_close",
        owner_id="user_alice",
        bot_id="bot_1",
        objective="完成发布流程",
        project_root=project_root,
    )

    notifications = []
    with patch.object(global_notifier, "push", side_effect=lambda **kwargs: notifications.append(kwargs)):
        res = await run_project_patrol(binding=binding, goal_run_id=goal_id, project_root=project_root)
        assert res["verdict"] == "done"
        # Must have requested approval
        assert any(
            n["type"] == "ACTION_REQUIRED" and "收尾动作待审批" in n["title"]
            for n in notifications
        )


@pytest.mark.asyncio
async def test_patrol_stalled_restart_and_exhausted(brain_env, tmp_path: Path):
    project_root = str(tmp_path)
    goal_id = "goal_stall"
    task_id = "ptask_stall"

    ptask = ProjectTask(
        task_id=task_id,
        objective="长耗时计算任务",
        goal_run_id=goal_id,
        binding_id="bind_stall",
        status=TASK_STATUS_RUNNING,
    )
    upsert_task(project_root, ptask)

    gstate = GoalRunState(goal_id=goal_id, goal_text="长耗时计算任务", bot_id="bot_1")
    gstate.tasks["t1"] = TaskNode(
        id="t1", title="计算", instruction="运行", acceptance=[], depends_on=[], assignee="bot_1", status=TaskStatus.running
    )
    save_goal_run(gstate, project_root)

    binding = SupervisorBinding(
        binding_id="bind_stall",
        goal_run_id=goal_id,
        routine_id="rout_stall",
        owner_id="user_alice",
        bot_id="bot_1",
        objective="长耗时计算任务",
        project_root=project_root,
        stall_after_minutes=5,
        max_repairs=2,
        nudges_sent=0,
        last_activity_at=time.time() - 600,  # 10 minutes ago -> stalled
    )

    # Cycle 1: Stalled -> restart nudge (under max_repairs) + WIP snapshot
    res1 = await run_project_patrol(binding=binding, goal_run_id=goal_id, project_root=project_root)
    assert res1["verdict"] == "stalled"
    assert binding.nudges_sent == 1
    wip_file = Path(project_root) / ".veya-project" / "runs" / task_id / "wip_snapshot.json"
    assert wip_file.exists()

    # Cycle 2: Stalled again -> reaches max_repairs=2 -> exhausts and stops
    binding.last_activity_at = time.time() - 600
    binding.nudges_sent = 2
    notifications = []
    with patch.object(global_notifier, "push", side_effect=lambda **kwargs: notifications.append(kwargs)):
        res2 = await run_project_patrol(binding=binding, goal_run_id=goal_id, project_root=project_root)
        assert res2["verdict"] == "stalled"
        assert binding.status == "closed"
        assert any(n["type"] == "WARNING" and "任务停滞需人工干预" in n["title"] for n in notifications)


@pytest.mark.asyncio
async def test_patrol_silence_strategy(brain_env, tmp_path: Path):
    project_root = str(tmp_path)
    goal_id = "goal_silent"
    task_id = "ptask_silent"

    ptask = ProjectTask(
        task_id=task_id,
        objective="平稳后台运行任务",
        goal_run_id=goal_id,
        binding_id="bind_silent",
        status=TASK_STATUS_RUNNING,
    )
    upsert_task(project_root, ptask)

    gstate = GoalRunState(goal_id=goal_id, goal_text="平稳后台运行任务", bot_id="bot_1")
    gstate.tasks["t1"] = TaskNode(
        id="t1", title="任务", instruction="运行", acceptance=[], depends_on=[], assignee="bot_1", status=TaskStatus.running
    )
    save_goal_run(gstate, project_root)

    binding = SupervisorBinding(
        binding_id="bind_silent",
        goal_run_id=goal_id,
        routine_id="rout_silent",
        owner_id="user_alice",
        bot_id="bot_1",
        objective="平稳后台运行任务",
        project_root=project_root,
        stall_after_minutes=60,
        last_report_fingerprint="0/1|0",
        last_activity_at=time.time(),
    )

    notifications = []
    with patch.object(global_notifier, "push", side_effect=lambda **kwargs: notifications.append(kwargs)):
        # Cycle 1: no new progress -> silent
        res1 = await run_project_patrol(binding=binding, goal_run_id=goal_id, project_root=project_root)
        assert res1["verdict"] in ("running", "progress")
        assert len(notifications) == 0
        t = get_task(project_root, task_id)
        assert t.receipts["_patrol"]["no_progress_cycles"] == 1

        # Cycle 2: no new progress -> silent
        res2 = await run_project_patrol(binding=binding, goal_run_id=goal_id, project_root=project_root)
        assert len(notifications) == 0
        t = get_task(project_root, task_id)
        assert t.receipts["_patrol"]["no_progress_cycles"] == 2

        # Cycle 3: 3 consecutive cycles with no progress -> notifies user
        res3 = await run_project_patrol(binding=binding, goal_run_id=goal_id, project_root=project_root)
        assert len(notifications) == 1
        assert notifications[0]["type"] == "INFO"
        assert "持续无新进展" in notifications[0]["title"]
        t = get_task(project_root, task_id)
        assert t.receipts["_patrol"]["no_progress_cycles"] == 0
