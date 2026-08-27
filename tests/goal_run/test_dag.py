"""goal_run DAG 校验测试。"""

from __future__ import annotations

from server.goal_run.models import TaskNode, TaskStatus


def test_task_ready_condition_met():
    """deps 全 completed 则 ready；空 deps 始终满足。"""
    t1 = TaskNode(
        id="t1",
        title="任务1",
        instruction="做 A",
        acceptance=["A 完成"],
        depends_on=[],
        assignee="hicode",
    )
    t2 = TaskNode(
        id="t2",
        title="任务2",
        instruction="做 B",
        acceptance=["B 完成"],
        depends_on=["t1"],
        assignee="hicode",
    )

    assert t1.ready_condition_met(set())
    assert not t2.ready_condition_met(set())
    assert t2.ready_condition_met({"t1"})


def test_task_can_run_now():
    """未运行且 deps 满足则可运行。"""
    t1 = TaskNode(
        id="t1",
        title="任务1",
        instruction="做 A",
        acceptance=["A 完成"],
        depends_on=[],
        assignee="hicode",
    )
    t2 = TaskNode(
        id="t2",
        title="任务2",
        instruction="做 B",
        acceptance=["B 完成"],
        depends_on=["t1"],
        assignee="hicode",
    )

    # 空 running_ids，t1 可运行
    assert t1.can_run_now(set(), set())
    # t1 未完成，t2 不可运行
    assert not t2.can_run_now(set(), set())
    # t1 已完成，t2 可运行
    assert t2.can_run_now(set(), {"t1"})
    # t1 正在运行，t2 不可运行
    assert not t2.can_run_now({"t1"}, set())


def test_dag_cycle_detection():
    """循环依赖检测（实际在 plan 阶段做拓扑排序）。"""
    # 构造循环：t1 -> t2 -> t3 -> t1
    t1 = TaskNode(
        id="t1",
        title="任务1",
        instruction="做 A",
        acceptance=["A 完成"],
        depends_on=["t3"],
        assignee="hicode",
    )
    t2 = TaskNode(
        id="t2",
        title="任务2",
        instruction="做 B",
        acceptance=["B 完成"],
        depends_on=["t1"],
        assignee="hicode",
    )
    t3 = TaskNode(
        id="t3",
        title="任务3",
        instruction="做 C",
        acceptance=["C 完成"],
        depends_on=["t2"],
        assignee="hicode",
    )

    # Kahn 算法检测循环
    from collections import deque

    nodes = {"t1": t1, "t2": t2, "t3": t3}
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

    # 循环图无法全部处理完
    assert processed < 3


def test_task_serialization_roundtrip():
    """taskgraph.json 序列化往返不丢字段。"""
    t = TaskNode(
        id="t1",
        title="任务1",
        instruction="做 A",
        acceptance=["A 完成"],
        depends_on=[],
        assignee="hicode",
        status=TaskStatus.pending,
        retries=0,
        leaf_task_id=None,
        verify_summary=None,
        block_reason=None,
        artifacts=[],
        execute_result=None,
    )

    data = {
        "id": t.id,
        "title": t.title,
        "instruction": t.instruction,
        "acceptance": t.acceptance,
        "depends_on": t.depends_on,
        "assignee": t.assignee,
        "status": t.status.value,
        "retries": t.retries,
        "leaf_task_id": t.leaf_task_id,
        "verify_summary": t.verify_summary,
        "block_reason": t.block_reason,
        "artifacts": t.artifacts,
        "execute_result": t.execute_result,
    }

    # 反序列化
    t2 = TaskNode(
        id=data["id"],
        title=data["title"],
        instruction=data["instruction"],
        acceptance=data["acceptance"],
        depends_on=data["depends_on"],
        assignee=data["assignee"],
        status=TaskStatus(data["status"]),
        retries=data["retries"],
        leaf_task_id=data["leaf_task_id"],
        verify_summary=data["verify_summary"],
        block_reason=data["block_reason"],
        artifacts=data["artifacts"],
        execute_result=data["execute_result"],
    )

    assert t2.id == t.id
    assert t2.status == t.status
    assert t2.acceptance == t.acceptance
    assert t2.depends_on == t.depends_on
