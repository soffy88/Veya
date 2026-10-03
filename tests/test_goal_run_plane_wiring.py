"""goal_run 平面接线证明 — P0-04 / P0-08 / §24 / §25 / §27 / P1-03.

这些模块早已写好但没有生产调用点，等于死代码。本文件对每个模块证明两件事:

1. **调用路径真的存在**: 用 AST 断言 runner.py 的生产函数体内确实调用了它
   (而不是只在注释或 import 里出现)。
2. **接缝行为正确**: 对接缝本身做聚焦单测, 并端到端跑一次
   ``project_run_goal``, 断言最终状态、事件记录与落盘状态。

不变量: 只能收紧, 不能放松。任何非成功的子任务、任何未完成的工作、任何
NO_PROGRESS_DETECTED 都不允许被上报成成功。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from server.completion_proposal import CompletionDecisionValue
from server.failure_semantics import TerminalState, is_non_success
from server.goal_run import ContinuationTriggerManager as ExportedTriggerManager
from server.goal_run.continuation import (
    ContinuationTriggerManager,
    ContinuationTriggerStore,
    TriggerPolicy,
    TriggerStatus,
    TriggerType,
)
from server.goal_run.leaf import LeafResult
from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus
from server.goal_run.runner import (
    _cancel_pending_continuation_triggers,
    _decide_goal_completion,
    _run_version_record,
    _select_task_topology,
    project_run_goal,
)
from server.goal_run.topology_selector import TopologyMode
from server.goal_run.verify import VerifyResult
from server.no_progress import NoProgressVerdict, ProgressSignal, ProgressTracker

TRIGGER_FILE = "continuation_triggers_isolated.json"


# ── 隔离: runner 的旁路写入不得落到开发者真实 ~/.veya ────────────────────
@pytest.fixture(autouse=True)
def _isolate_runner_side_channels(tmp_path, monkeypatch):
    """同 tests/goal_run/conftest.py 的理由: PerformanceStore / MemoryController /
    durable runtime / continuation trigger store 默认都指向用户生产文件。"""
    from runtime.execution.runtime import DurableExecutionRuntime, DurableRuntimeConfig
    from server.capability_model import PerformanceStore
    from server.goal_run import runner as runner_module
    from server.memory_controller import MemoryController, _MemoryStore

    monkeypatch.setattr(
        runner_module,
        "performance_store",
        PerformanceStore(storage_path=tmp_path / "perf_store_isolated.jsonl"),
    )
    monkeypatch.setattr(
        runner_module,
        "memory_controller",
        MemoryController(_MemoryStore(storage_path=tmp_path / "memory_isolated.json")),
    )
    monkeypatch.setattr(
        "runtime.execution.runtime._default_runtime",
        DurableExecutionRuntime(
            DurableRuntimeConfig(enabled=False, sqlite_path=str(tmp_path / "durable.sqlite3"))
        ),
    )
    # P0-04: 生产调用点走 ContinuationTriggerManager(); 这里把它钉在 tmp 上,
    # 使测试既不改写也不读取真实 ~/.veya/continuation_triggers.json。
    monkeypatch.setattr(
        runner_module,
        "ContinuationTriggerManager",
        lambda: ContinuationTriggerManager(ContinuationTriggerStore(tmp_path / TRIGGER_FILE)),
    )


@pytest.fixture
def trigger_store(tmp_path) -> ContinuationTriggerStore:
    return ContinuationTriggerStore(tmp_path / TRIGGER_FILE)


# ── AST 断言: 生产函数体内真的有调用 ─────────────────────────────────────
def _function_node(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    from server.goal_run import runner as runner_module

    tree = ast.parse(Path(runner_module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"runner.py no longer defines {name}")


def _called(node: ast.AST) -> set[str]:
    """函数体里真正被调用的名字; ``obj.method(...)`` 折叠为 ``obj.method``。"""
    found: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            found.add(func.id)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            found.add(f"{func.value.id}.{func.attr}")
    return found


def test_p008_completion_seam_calls_the_proposal_contract():
    """P0-08: CompletionProposal/VerificationPlan/decide() 在完成判定接缝里。"""
    called = _called(_function_node("_decide_goal_completion"))
    assert "decide" in called
    assert "AcceptanceCriteria" in called
    assert "new_verification_plan" in called
    assert "new_proposal" in called
    assert "new_evidence" in called


def test_failure_semantics_owns_the_parent_aggregation():
    """§24: 父状态由 reconcile_parent/is_success 推导, 不再是临时的布尔表达式。"""
    called = _called(_function_node("_decide_goal_completion"))
    assert "reconcile_parent" in called
    assert "is_success" in called
    # 旧实现里的 has_partial_work 局部变量不得复活
    finalize = ast.dump(_function_node("_run_loop_and_finalize"))
    assert "has_partial_work" not in finalize


def test_no_progress_tracker_runs_inside_the_task_loop():
    """§25: ProgressTracker 在 G2 循环里真的被实例化、tick、record。"""
    loop = _function_node("_run_loop_and_finalize")
    called = _called(loop)
    assert "ProgressTracker" in called
    assert "progress_tracker.tick" in called
    assert "progress_tracker.record" in called
    # NO_PROGRESS_DETECTED 必须一路传进完成判定, 否则只是空转检测
    decision_calls = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_decide_goal_completion"
    ]
    assert len(decision_calls) == 1
    assert "semantic_no_progress" in {
        keyword.arg for keyword in decision_calls[0].keywords if keyword.arg
    }


def test_version_record_is_attached_at_finalization():
    """§27: VersionRecord 在 finalization 路径上被构造并写回 run state。"""
    assert "VersionRecord" in _called(_function_node("_run_version_record"))
    finalize = _function_node("_run_loop_and_finalize")
    assert "_run_version_record" in _called(finalize)
    # 必须真的挂到 state 上, 而不是构造完就丢
    assigned_attrs = {
        target.attr
        for node in ast.walk(finalize)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
    }
    assert "version_record" in assigned_attrs


def test_topology_selector_runs_where_per_task_execution_is_decided():
    """P1-03: selector.select(TopologyContext) 在逐任务派发处被调用。"""
    assert "TopologyContext" in _called(_function_node("_topology_context_for_task"))
    assert "_TOPOLOGY_SELECTOR.select" in _called(_function_node("_select_task_topology"))
    dispatch_loop = _called(_function_node("_run_loop_and_finalize"))
    assert "_select_task_topology" in dispatch_loop


def test_continuation_trigger_manager_is_wired_without_a_scheduler():
    """P0-04: 终态 run 取消自己的 pending trigger; 没有新增 admission 路径。"""
    from server.goal_run import runner as runner_module

    assert "ContinuationTriggerManager" in _called(
        _function_node("_cancel_pending_continuation_triggers")
    )
    assert "_cancel_pending_continuation_triggers" in _called(
        _function_node("_run_loop_and_finalize")
    )
    # claim_next / create_trigger 才是 admission 侧; 终态只允许 cancel。
    assert "claim_next" not in _called(_function_node("_cancel_pending_continuation_triggers"))
    assert "create_trigger" not in _called(_function_node("_cancel_pending_continuation_triggers"))
    # 包级导出遵循既有约定 (从子模块 re-export 到 __all__)
    assert ExportedTriggerManager is ContinuationTriggerManager
    import server.goal_run as goal_run_pkg

    assert "ContinuationTriggerManager" in goal_run_pkg.__all__
    # runner 模块确实 import 了它 (不是靠 getattr 兜底)
    source = Path(runner_module.__file__).read_text(encoding="utf-8")
    assert "from server.goal_run.continuation import ContinuationTriggerManager" in source


# ── 接缝行为: §24 + P0-08 ────────────────────────────────────────────────
def _state_with(task_statuses: dict[str, TaskStatus], **kwargs) -> GoalRunState:
    state = GoalRunState(goal_id="g-wiring", goal_text="wiring goal")
    for task_id, status in task_statuses.items():
        node = TaskNode(
            id=task_id,
            title=task_id,
            instruction=task_id,
            acceptance=["ok"],
            depends_on=[],
            assignee="hicode",
        )
        node.status = status
        state.tasks[task_id] = node
        if status is TaskStatus.completed:
            state.completed_ids.add(task_id)
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


def _decide(state: GoalRunState, **kwargs):
    total = len(state.tasks)
    completed = sum(1 for t in state.tasks.values() if t.status is TaskStatus.completed)
    blocked = sum(1 for t in state.tasks.values() if t.status is TaskStatus.blocked)
    cancelled = sum(1 for t in state.tasks.values() if t.status is TaskStatus.cancelled)
    return _decide_goal_completion(
        state,
        total=total,
        completed=completed,
        blocked=blocked,
        cancelled=cancelled,
        **kwargs,
    )


def test_all_children_succeeded_is_accepted():
    status, verdict, record = _decide(_state_with({"a": TaskStatus.completed}))
    assert status is GoalStatus.completed
    assert verdict == "ACCEPT"
    assert record["terminal_state"] == "SUCCEEDED"
    assert record["decision"]["outcome"] == CompletionDecisionValue.ACCEPT.value
    # AcceptanceCriteria → VerificationPlan 的派生关系是真实的
    assert record["criteria"]["goal_run_id"] == "g-wiring"
    assert record["verification_plan"]["checks"] == record["criteria"]["checks"]
    assert record["verification_plan"]["required_evidence"]
    assert record["evidence"][0]["check"] in record["verification_plan"]["checks"]
    assert record["proposal"]["goal_run_id"] == "g-wiring"


@pytest.mark.parametrize(
    ("child_status", "expected_terminal"),
    [
        (TaskStatus.blocked, "BLOCKED"),
        (TaskStatus.cancelled, "CANCELLED"),
        # A child that never reached a terminal state is INTERRUPTED; §24 has
        # no INTERRUPTED branch, so it reconciles to the generic FAILED.
        (TaskStatus.pending, "FAILED"),
        (TaskStatus.running, "FAILED"),
        (TaskStatus.verifying, "FAILED"),
        (TaskStatus.ready, "FAILED"),
    ],
)
def test_non_success_child_can_never_yield_parent_success(child_status, expected_terminal):
    """非成功子任务绝不能通过接缝变成父级成功。"""
    state = _state_with({"good": TaskStatus.completed, "bad": child_status})
    status, verdict, record = _decide(state)

    assert status is GoalStatus.partial_completed
    assert status is not GoalStatus.completed
    assert verdict == "PARTIAL"
    assert record["terminal_state"] == expected_terminal
    assert record["decision"]["outcome"] != CompletionDecisionValue.ACCEPT.value
    assert is_non_success(TerminalState(record["terminal_state"]))


def test_empty_task_graph_is_not_a_success():
    """空任务图: reconcile_parent([]) = FAILED, 不再上报 completed。"""
    status, verdict, record = _decide(_state_with({}))
    assert status is GoalStatus.partial_completed
    assert verdict == "PARTIAL"
    assert record["terminal_state"] == "FAILED"
    assert record["decision"]["outcome"] == "NEEDS_MORE_WORK"


def test_unfinished_work_blocks_accept_even_when_every_child_succeeded():
    """blockers 不是装饰: 全子任务成功但仍有未完成工作时必须拒绝 ACCEPT。"""
    state = _state_with({"a": TaskStatus.completed}, unfinished_work=["a-retry"])
    status, verdict, record = _decide(state)

    assert status is GoalStatus.partial_completed
    assert verdict == "PARTIAL"
    assert record["terminal_state"] == "SUCCEEDED"
    assert record["decision"]["outcome"] == "BLOCKED"
    assert "unfinished_work=1" in record["decision"]["reason"]


def test_semantic_no_progress_refuses_success():
    """§25: NO_PROGRESS_DETECTED 之后绝不能上报成功, 即使所有子任务都成功。"""
    state = _state_with({"a": TaskStatus.completed})
    status, verdict, record = _decide(state, semantic_no_progress=True)

    assert status is GoalStatus.partial_completed
    assert verdict == "PARTIAL"
    assert "no_progress_detected" in record["decision"]["reason"]
    assert "no_progress_detected" in record["proposal"]["summary"]


def test_progress_tracker_verdict_is_real():
    """ProgressTracker: 记录进展后保持 PROGRESSING, 空转到阈值后拒绝继续。"""
    tracker = ProgressTracker(goal_run_id="g-wiring")
    for _ in range(3):
        tracker.record(ProgressSignal.NEW_TASK_COMPLETION)
        assert tracker.tick() is NoProgressVerdict.PROGRESSING
    for _ in range(4):
        tracker.tick()
    assert tracker.tick() is NoProgressVerdict.NO_PROGRESS_DETECTED


# ── 接缝行为: §27 ───────────────────────────────────────────────────────
def test_version_record_reports_only_pins_the_run_actually_has():
    bare = _run_version_record(_state_with({}))
    assert bare.goal_run_id == "g-wiring"
    # 未解析的组件保持为空, 不猜
    assert bare.agent_spec_version == ""
    assert bare.skill_versions == {}
    assert bare.workflow_versions == {}
    assert bare.to_dict()["goal_run_id"] == "g-wiring"

    pinned = _state_with(
        {},
        agent_definition_version=7,
        active_skill_id="brief.render",
        active_skill_version=2,
        active_playbook_version=3,
    )
    record = _run_version_record(pinned)
    assert record.agent_spec_version == "7"
    assert record.skill_versions == {"brief.render": "2"}
    assert record.workflow_versions == {"playbook": "3"}


def test_version_record_survives_the_state_round_trip():
    state = _state_with({"a": TaskStatus.completed}, agent_definition_version=4)
    state.version_record = _run_version_record(state).to_dict()

    restored = GoalRunState.from_taskgraph_json(state.to_taskgraph_json(), state.goal_text)
    assert restored.version_record == state.version_record
    assert restored.version_record["agent_spec_version"] == "4"


# ── 接缝行为: P1-03 ──────────────────────────────────────────────────────
def _task(**kwargs) -> TaskNode:
    defaults = {
        "id": "t",
        "title": "t",
        "instruction": "t",
        "acceptance": ["ok"],
        "depends_on": [],
        "assignee": "hicode",
    }
    return TaskNode(**{**defaults, **kwargs})


def test_topology_suggestion_follows_declared_task_shape():
    parallel_root = _select_task_topology(_task(parallel=True))
    assert parallel_root.mode is TopologyMode.TEAM
    assert parallel_root.confidence > 0.0
    assert parallel_root.reason

    serial_leaf = _select_task_topology(_task(parallel=False, depends_on=["root"]))
    assert serial_leaf.mode is TopologyMode.INLINE
    # 串行叶子确实建议不升级拓扑
    assert serial_leaf.mode is not TopologyMode.TEAM


def test_topology_context_uses_only_declared_facts():
    from server.goal_run.runner import _topology_context_for_task

    context = _topology_context_for_task(_task(parallel=True, depends_on=["a", "b"]))
    assert context.parallelizability == 1.0
    # 声明的依赖折算成 independence
    assert context.independence == 0.5
    assert context.required_capabilities == ["hicode"]
    assert context.workspace_conflict_risk == 0.0
    # 未估计的因子保持 0, 不编造
    assert context.duration_estimate == 0.0
    assert context.expected_tool_noise == 0.0
    assert context.human_interaction_requirement == 0.0


# ── 接缝行为: P0-04 ──────────────────────────────────────────────────────
def test_terminal_run_cancels_its_own_pending_continuation_triggers(trigger_store):
    manager = ContinuationTriggerManager(trigger_store)
    manager.create_trigger(
        goal_run_id="g-wiring",
        trigger_type=TriggerType.MANUAL_RESUME,
        next_due_at=0.0,
        policy=TriggerPolicy.ONE_SHOT,
    )
    manager.create_trigger(
        goal_run_id="other-goal", trigger_type=TriggerType.HEARTBEAT, next_due_at=0.0
    )
    assert len(trigger_store.list_for_goal("g-wiring")) == 1

    assert _cancel_pending_continuation_triggers("g-wiring") == 1
    remaining = trigger_store.list_for_goal("g-wiring")
    assert [t.status for t in remaining] == [TriggerStatus.CANCELLED]
    # 别的 run 的 trigger 不受影响
    assert [t.status for t in trigger_store.list_for_goal("other-goal")] == [TriggerStatus.PENDING]
    # 幂等: 第二次没有可取消的
    assert _cancel_pending_continuation_triggers("g-wiring") == 0


def test_trigger_cancel_failure_is_advisory_only(trigger_store):
    class _Broken:
        def cancel_for_goal(self, _goal_run_id: str) -> int:
            raise RuntimeError("store offline")

    import server.goal_run.runner as runner_module

    original = runner_module.ContinuationTriggerManager
    runner_module.ContinuationTriggerManager = lambda: _Broken()
    try:
        assert _cancel_pending_continuation_triggers("g-wiring") == 0
    finally:
        runner_module.ContinuationTriggerManager = original


# ── 端到端: 走真实 project_run_goal 主链路 ───────────────────────────────
async def _fake_leaf(*_args, **_kwargs) -> LeafResult:
    return LeafResult(status="completed", summary="done", artifacts=[])


async def _fake_verify(*_args, **_kwargs) -> VerifyResult:
    return VerifyResult(passed=True, summary="ok")


async def _fake_review(*_args, **_kwargs):
    return None


def _stub_run(monkeypatch, *, leaf=_fake_leaf) -> None:
    monkeypatch.setattr("server.goal_run.runner.execute_leaf_with_memory", leaf)
    monkeypatch.setattr("server.goal_run.runner.verify_task", _fake_verify)
    monkeypatch.setattr("server.goal_run.runner._run_dual_axis_review", _fake_review)
    monkeypatch.setenv("VEYA_GOAL_RUN_PLAN_REVIEW_ENABLED", "0")
    monkeypatch.setenv("VEYA_GOAL_RUN_CODE_REVIEW_ENABLED", "0")
    # 验收失败的重试会通过 SessionTreeMgr.branch() 落到用户级 session tree,
    # 属旁路追踪, 与本文件的接线断言无关, 显式关掉避免写生产库。
    monkeypatch.setenv("VEYA_GOAL_RUN_BRANCH_ENABLED", "0")


def _events(project_root: Path, goal_id: str) -> list[dict]:
    path = project_root / ".veya-project" / "goal-runs" / goal_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _taskgraph(project_root: Path, goal_id: str) -> dict:
    path = project_root / ".veya-project" / "goal-runs" / goal_id / "taskgraph.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_wired_run_records_decision_version_and_topology(tmp_path, monkeypatch):
    """成功路径: decision/ACCEPT、VersionRecord 落盘、topology 记录进事件。"""
    _stub_run(monkeypatch)

    result = await project_run_goal(
        project_root=str(tmp_path),
        goal="wiring success",
        mode="act_eager",
        tasks=[{"id": "A", "instruction": "A", "acceptance": ["ok"], "parallel": True}],
    )

    assert result.status == GoalStatus.completed
    assert result.phase == "finalized"

    graph = _taskgraph(tmp_path, result.goal_id)
    assert graph["acceptance_verdict"] == "ACCEPT"
    # §27 落盘
    assert graph["version_record"]["goal_run_id"] == result.goal_id
    assert "created_at" in graph["version_record"]

    events = _events(tmp_path, result.goal_id)
    finished = [e for e in events if e["type"] == "finalization.completed"]
    assert len(finished) == 1
    completion = finished[0]["completion"]
    assert completion["terminal_state"] == "SUCCEEDED"
    assert completion["decision"]["outcome"] == "ACCEPT"
    assert completion["verification_plan"]["checks"] == ["CUSTOM"]
    # P0-04 调用点在真实主链路上跑到了
    assert finished[0]["continuation_triggers_cancelled"] == 0

    # P1-03: [P] 根任务被建议 TEAM 拓扑, 但执行器没变 (leaf 仍然跑过)
    started = [e for e in events if e["type"] == "scheduler.task_started"]
    assert [e["task_id"] for e in started] == ["A"]
    assert started[0]["topology_mode"] == TopologyMode.TEAM.value
    assert started[0]["topology_escalation_suggested"] is True


@pytest.mark.asyncio
async def test_wired_non_success_child_cannot_report_parent_success(tmp_path, monkeypatch):
    """失败叶子 + 成功兄弟: 父级不得被上报成成功 (§24 + P0-08 端到端)。"""

    async def leaf(*, instruction: str, **_kwargs) -> LeafResult:
        # 重试分支会把上轮失败原因拼进 instruction, 所以按前缀判定。
        if instruction.startswith("bad"):
            return LeafResult(
                status="blocked",
                summary="",
                block_reason="worker crashed",
                artifacts=[],
                unfinished_work=["finish bad"],
            )
        return LeafResult(status="completed", summary="sibling done", artifacts=[])

    _stub_run(monkeypatch, leaf=leaf)

    result = await project_run_goal(
        project_root=str(tmp_path),
        goal="wiring mixed",
        mode="act_eager",
        tasks=[
            {"id": "good", "instruction": "good", "acceptance": ["ok"]},
            {"id": "bad", "instruction": "bad", "acceptance": ["ok"]},
        ],
    )

    assert result.status is GoalStatus.partial_completed
    assert result.status is not GoalStatus.completed

    graph = _taskgraph(tmp_path, result.goal_id)
    assert graph["status"] == "partial_completed"
    assert graph["acceptance_verdict"] == "PARTIAL"

    finished = [
        e for e in _events(tmp_path, result.goal_id) if e["type"] == "finalization.completed"
    ]
    completion = finished[0]["completion"]
    assert completion["terminal_state"] == "BLOCKED"
    assert completion["decision"]["outcome"] != "ACCEPT"
    assert "blocked_tasks=1" in completion["decision"]["reason"]
