"""server.trajectory — docs/VEYA_P1_P3_IMPLEMENTATION_SPEC.md §16 Trajectory。

范围边界：
- `server/goal_run/trust_plane.py` 的 `TaskEpisode`(Claim/Evidence/
  EvaluationResult/VerifiedState 全链路)只覆盖 GoalRun——那套结构依赖
  `verify_task`/双轴 review 的产物，普通 MasterAgent 聊天轮次和
  `agent_loop_run` 委托子任务都没有这些概念，硬套会编造不存在的数据。
  本模块是给"非 GoalRun 路径"的一个更轻量、字段对齐规格 §16 的独立结构，
  不是 TaskEpisode 的替代品，两者各自服务不同的执行路径。
- 已接入 `MasterCoordinator.chat_stream()` 与
  `server/agent_loop_bridge.py::run_strict_chat`；两条路径只在轮末记录已经发生的
  工具事实，不参与主链决策。`acceptance_results`/`recovery_actions` 没有对应事实
  时保持空列表；主脑轮次使用 TaskStore 生成的 trace_id，隔离 AgentLoop 子任务
  在没有 trace context 时保持空值。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Trajectory:
    """docs/VEYA_P1_P3_IMPLEMENTATION_SPEC.md §16 定义的结构，字段名逐一对齐。"""

    task_id: str
    objective: str

    steps: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)

    outcome: str = "completed"  # completed | failed
    acceptance_results: list[dict] = field(default_factory=list)

    failures: list[dict] = field(default_factory=list)
    recovery_actions: list[dict] = field(default_factory=list)

    cost_usd: float = 0.0
    duration_ms: int = 0

    trace_id: str = ""
    schema_version: int = 1


def build_trajectory(
    *,
    task_id: str,
    objective: str,
    outcome: str,
    tool_calls: list[dict],
    duration_ms: int,
    error: str | None = None,
    steps: list[dict] | None = None,
    acceptance_results: list[dict] | None = None,
    recovery_actions: list[dict] | None = None,
    cost_usd: float = 0.0,
    trace_id: str = "",
) -> Trajectory:
    """纯函数：从调用方已经算出来的结果拼一份 Trajectory，不做任何 IO。"""
    return Trajectory(
        task_id=task_id,
        objective=objective,
        tool_calls=list(tool_calls),
        steps=list(steps or []),
        outcome=outcome,
        acceptance_results=list(acceptance_results or []),
        failures=[{"error": error}] if error else [],
        recovery_actions=list(recovery_actions or []),
        cost_usd=float(cost_usd or 0.0),
        duration_ms=duration_ms,
        trace_id=trace_id,
    )


def _default_path(task_id: str) -> Path:
    return Path.home() / ".veya" / "trajectories" / f"{task_id}.jsonl"


def append_trajectory(trajectory: Trajectory, *, path: Path | None = None) -> None:
    """追加写入一条 trajectory 记录 (JSONL, 跟 trust_plane.jsonl/events.jsonl 同一模式)。"""
    target = path or _default_path(trajectory.task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(trajectory), ensure_ascii=False) + "\n")
    # Learning/Eval 侧只消费事实事件；落盘失败不能让主链失败，但记录事件时
    # 也不要求调用方额外维护一条旁路。
    try:
        from server.events import append_canonical_event

        append_canonical_event(
            "trajectory.recorded",
            asdict(trajectory),
            actor="system",
            trace_id=trajectory.trace_id or trajectory.task_id,
            task_id=trajectory.task_id,
        )
        if trajectory.acceptance_results:
            passed = all(
                str(item.get("status", "")).lower() in {"passed", "pass", "ok"}
                for item in trajectory.acceptance_results
                if isinstance(item, dict)
            )
            append_canonical_event(
                "eval.recorded",
                {
                    "evaluator": "acceptance",
                    "task_id": trajectory.task_id,
                    "passed": passed,
                    "criteria_count": len(trajectory.acceptance_results),
                    "outcome": trajectory.outcome,
                },
                actor="system",
                trace_id=trajectory.trace_id or trajectory.task_id,
                task_id=trajectory.task_id,
            )
    except Exception:
        pass


def read_trajectories(task_id: str, *, path: Path | None = None) -> list[dict[str, Any]]:
    target = path or _default_path(task_id)
    if not target.exists():
        return []
    lines = target.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines if line.strip()]
