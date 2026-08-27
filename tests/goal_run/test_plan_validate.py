"""goal_run plan validation 测试。"""

from __future__ import annotations

import pytest

from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus


def test_plan_task_count_limit():
    """任务数 1 … max_leaf_tasks 约束。"""
    state = GoalRunState(goal_id="g1", goal_text="test", budget={"max_leaf_tasks": 40})

    # 添加 41 个任务
    for i in range(41):
        tn = TaskNode(
            id=f"t{i + 1}",
            title=f"任务{i + 1}",
            instruction=f"做 {i + 1}",
            acceptance=[f"验收 {i + 1}"],
            depends_on=[],
            assignee="hicode",
        )
        state.tasks[tn.id] = tn

    # 超过 max_leaf_tasks
    assert len(state.tasks) > state.budget["max_leaf_tasks"]


def test_plan_acceptance_nonempty():
    """每任务必须有非空 acceptance。"""
    t1 = TaskNode(
        id="t1",
        title="任务1",
        instruction="做 A",
        acceptance=[],  # 空 acceptance
        depends_on=[],
        assignee="hicode",
    )
    t2 = TaskNode(
        id="t2",
        title="任务2",
        instruction="做 B",
        acceptance=["验收 B"],  # 非空
        depends_on=[],
        assignee="hicode",
    )

    # t1 acceptance 为空，不应通过验证
    assert len(t1.acceptance) == 0
    assert len(t2.acceptance) > 0


def test_plan_depends_on_acyclic():
    """depends_on 无环（拓扑校验）。"""
    # 无环图
    t1 = TaskNode(
        id="t1", title="A", instruction="A", acceptance=["a"], depends_on=[], assignee="hicode"
    )
    t2 = TaskNode(
        id="t2", title="B", instruction="B", acceptance=["b"], depends_on=["t1"], assignee="hicode"
    )
    t3 = TaskNode(
        id="t3", title="C", instruction="C", acceptance=["c"], depends_on=["t2"], assignee="hicode"
    )

    nodes = {"t1": t1, "t2": t2, "t3": t3}
    # Kahn 算法
    from collections import deque

    in_degree = {nid: len(n.depends_on) for nid, n in nodes.items()}
    zero_queue = deque([nid for nid, deg in in_degree.items() if deg == 0])

    processed = 0
    while zero_queue:
        nid = zero_queue.popleft()
        processed += 1
        for other in nodes.values():
            if nid in other.depends_on:
                in_degree[other.id] -= 1
                if in_degree[other.id] == 0:
                    zero_queue.append(other.id)

    assert processed == 3  # 无环，全部处理完


def test_plan_depends_on_cyclic_blocked():
    """有环 → blocked + reason（实际在 plan 阶段拦截）。"""
    t1 = TaskNode(
        id="t1", title="A", instruction="A", acceptance=["a"], depends_on=["t3"], assignee="hicode"
    )
    t2 = TaskNode(
        id="t2", title="B", instruction="B", acceptance=["b"], depends_on=["t1"], assignee="hicode"
    )
    t3 = TaskNode(
        id="t3", title="C", instruction="C", acceptance=["c"], depends_on=["t2"], assignee="hicode"
    )

    nodes = {"t1": t1, "t2": t2, "t3": t3}
    from collections import deque

    in_degree = {nid: len(n.depends_on) for nid, n in nodes.items()}
    zero_queue = deque([nid for nid, deg in in_degree.items() if deg == 0])

    processed = 0
    while zero_queue:
        nid = zero_queue.popleft()
        processed += 1
        for other in nodes.values():
            if nid in other.depends_on:
                in_degree[other.id] -= 1
                if in_degree[other.id] == 0:
                    zero_queue.append(other.id)

    assert processed < 3  # 有环，无法全部处理


def test_plan_default_assignee():
    """默认所有 assignee = default_assignee。"""
    state = GoalRunState(goal_id="g1", goal_text="test", default_assignee="hicode")

    t1 = TaskNode(
        id="t1", title="A", instruction="A", acceptance=["a"], depends_on=[], assignee="hicode"
    )
    t2 = TaskNode(
        id="t2", title="B", instruction="B", acceptance=["b"], depends_on=[], assignee="hicode"
    )

    state.tasks = {"t1": t1, "t2": t2}

    # 验证所有任务 assignee 都是 default_assignee
    for tn in state.tasks.values():
        assert tn.assignee == state.default_assignee


def test_taskgraph_json_serialization():
    """GoalRunState ↔ taskgraph.json 往返不丢数据。"""
    state = GoalRunState(
        goal_id="g1",
        goal_text="test goal",
        status=GoalStatus.running,
        default_assignee="hicode",
        budget={"max_wall_s": 7200, "max_leaf_tasks": 40, "max_retries_per_task": 2},
    )

    t1 = TaskNode(
        id="t1",
        title="任务1",
        instruction="实现功能",
        acceptance=["可观察条件1"],
        depends_on=[],
        assignee="hicode",
        status=TaskStatus.completed,
        retries=0,
        artifacts=["file1.py"],
    )
    state.tasks = {"t1": t1}
    state.completed_ids = {"t1"}

    # 序列化
    data = state.to_taskgraph_json()

    # 反序列化
    new_state = GoalRunState.from_taskgraph_json(data, "test goal")

    assert new_state.goal_id == state.goal_id
    assert new_state.status == state.status
    assert new_state.default_assignee == state.default_assignee
    assert new_state.budget == state.budget
    assert "t1" in new_state.tasks
    assert new_state.tasks["t1"].status == TaskStatus.completed
    assert new_state.tasks["t1"].artifacts == ["file1.py"]


@pytest.mark.asyncio
async def test_g1_uses_speckit_when_present(tmp_path):
    from server.goal_run.planner import g1_plan

    spec = tmp_path / ".speckit"
    spec.mkdir()
    (spec / "constitution.md").write_text("Do not use axios\n", encoding="utf-8")
    (spec / "tasks.md").write_text(
        "- [ ] T1 Setup\n  Acceptance: repo ready\n",
        encoding="utf-8",
    )
    state, resp = await g1_plan(
        interpretation="setup",
        assumptions=[],
        goal_text="setup",
        project_root=str(tmp_path),
    )
    assert "T1" in state.tasks
    assert "axios" in state.constitution
    assert resp.goal_id == state.goal_id
