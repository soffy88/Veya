"""goal_run scheduler 测试。"""

from __future__ import annotations

import pytest

from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus, TaskNode
from server.goal_run.scheduler import SchedulerState, pick_next_tasks


def test_scheduler_promote_ready():
    """deps 满足的 pending → ready。"""
    state = GoalRunState(goal_id="g1", goal_text="test")
    t1 = TaskNode(
        id="t1", title="A", instruction="A", acceptance=["a"], depends_on=[], assignee="hicode"
    )
    t2 = TaskNode(
        id="t2", title="B", instruction="B", acceptance=["b"], depends_on=["t1"], assignee="hicode"
    )
    state.tasks = {"t1": t1, "t2": t2}
    state.completed_ids = set()
    state.running_ids = set()

    sched = SchedulerState(state=state, max_concurrent=1)
    from server.goal_run.scheduler import SchedulerDecision

    decision = sched.promote_ready()

    # t1 deps 空 -> ready
    assert decision == SchedulerDecision.promote
    assert t1.status == TaskStatus.ready
    # t2 deps 未满足 -> 仍 pending
    assert t2.status == TaskStatus.pending


def test_scheduler_all_done():
    """所有任务完成 → all_done。"""
    state = GoalRunState(goal_id="g1", goal_text="test")
    t1 = TaskNode(
        id="t1", title="A", instruction="A", acceptance=["a"], depends_on=[], assignee="hicode"
    )
    t1.status = TaskStatus.completed
    state.tasks = {"t1": t1}
    state.completed_ids = {"t1"}
    state.running_ids = set()

    sched = SchedulerState(state=state, max_concurrent=1)
    from server.goal_run.scheduler import SchedulerDecision

    decision = sched.promote_ready()
    assert decision == SchedulerDecision.all_done


def test_pick_next_tasks_non_parallel_never_batches(monkeypatch=None):
    """非 [P] 任务永远独占一个批次, 即使 max_concurrent>1 也不会跟别的任务同批——
    parallel=False 是"没声明可以安全并行", veya 不替它猜(见 memory
    project_veya_pi_gap_audit smart-ralph [P] 内化)。"""
    state = GoalRunState(goal_id="g1", goal_text="test")
    t1 = TaskNode(
        id="t1", title="A", instruction="A", acceptance=["a"], depends_on=[], assignee="hicode"
    )
    t1.status = TaskStatus.ready
    t2 = TaskNode(
        id="t2", title="B", instruction="B", acceptance=["b"], depends_on=[], assignee="hicode"
    )
    t2.status = TaskStatus.ready
    t3 = TaskNode(
        id="t3", title="C", instruction="C", acceptance=["c"], depends_on=[], assignee="hicode"
    )
    t3.status = TaskStatus.ready
    state.tasks = {"t1": t1, "t2": t2, "t3": t3}
    state.completed_ids = set()
    state.running_ids = set()

    # max_concurrent=2, 但没一个任务标 [P] -> 只取队头这一个
    batch = pick_next_tasks(state, 2, state.running_ids)
    assert len(batch) == 1
    assert batch[0].id == "t1"
    assert batch[0].status == TaskStatus.running
    assert len(state.running_ids) == 1


def test_pick_next_tasks_parallel_marked_batches_up_to_concurrency():
    """[P] 标记的任务能同批跑, 但不超过 max_concurrent。"""
    state = GoalRunState(goal_id="g1", goal_text="test")
    t1 = TaskNode(
        id="t1",
        title="A",
        instruction="A",
        acceptance=["a"],
        depends_on=[],
        assignee="hicode",
        parallel=True,
    )
    t1.status = TaskStatus.ready
    t2 = TaskNode(
        id="t2",
        title="B",
        instruction="B",
        acceptance=["b"],
        depends_on=[],
        assignee="hicode",
        parallel=True,
    )
    t2.status = TaskStatus.ready
    t3 = TaskNode(
        id="t3",
        title="C",
        instruction="C",
        acceptance=["c"],
        depends_on=[],
        assignee="hicode",
        parallel=True,
    )
    t3.status = TaskStatus.ready
    state.tasks = {"t1": t1, "t2": t2, "t3": t3}
    state.completed_ids = set()
    state.running_ids = set()

    batch = pick_next_tasks(state, 2, state.running_ids)
    assert len(batch) == 2
    assert {t.id for t in batch} == {"t1", "t2"}
    assert all(t.status == TaskStatus.running for t in batch)
    assert len(state.running_ids) == 2


def test_pick_next_tasks_stops_at_first_non_parallel_in_queue():
    """[P] 批次从队头开始收集, 遇到第一个非 [P] 就停(不跳过抢跑, 保持公平)。"""
    state = GoalRunState(goal_id="g1", goal_text="test")
    t1 = TaskNode(
        id="t1",
        title="A",
        instruction="A",
        acceptance=["a"],
        depends_on=[],
        assignee="hicode",
        parallel=True,
    )
    t1.status = TaskStatus.ready
    t2 = TaskNode(
        id="t2",
        title="B",
        instruction="B",
        acceptance=["b"],
        depends_on=[],
        assignee="hicode",
        parallel=False,
    )
    t2.status = TaskStatus.ready
    t3 = TaskNode(
        id="t3",
        title="C",
        instruction="C",
        acceptance=["c"],
        depends_on=[],
        assignee="hicode",
        parallel=True,
    )
    t3.status = TaskStatus.ready
    state.tasks = {"t1": t1, "t2": t2, "t3": t3}
    state.completed_ids = set()
    state.running_ids = set()

    batch = pick_next_tasks(state, 4, state.running_ids)
    assert {t.id for t in batch} == {"t1"}


def test_pick_next_tasks_skips_running():
    """已 running 的任务不再被 pick。"""
    state = GoalRunState(goal_id="g1", goal_text="test")
    t1 = TaskNode(
        id="t1", title="A", instruction="A", acceptance=["a"], depends_on=[], assignee="hicode"
    )
    t1.status = TaskStatus.ready
    t2 = TaskNode(
        id="t2", title="B", instruction="B", acceptance=["b"], depends_on=[], assignee="hicode"
    )
    t2.status = TaskStatus.running  # 已在运行
    state.tasks = {"t1": t1, "t2": t2}
    state.completed_ids = set()
    state.running_ids = {"t2"}

    batch = pick_next_tasks(state, 2, state.running_ids)
    # 只会 pick t1，t2 已经在 running
    assert len(batch) == 1
    assert batch[0].id == "t1"


def test_scheduler_budget_check():
    """预算检查：超时返回 False。"""
    state = GoalRunState(goal_id="g1", goal_text="test", budget={"max_wall_s": 100})
    sched = SchedulerState(state=state, max_concurrent=1)

    assert sched.check_budget(50.0)  # 未超时
    assert not sched.check_budget(150.0)  # 超时
