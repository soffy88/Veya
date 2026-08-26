"""goal_run store — taskgraph.json / events.jsonl / GOAL.md 持久化。

目录结构（项目内 .veya-project/goal-runs/<goal_id>/）：
  GOAL.md          # 原目标 + interpretation + assumptions
  taskgraph.json   # 权威任务图（含状态、重试、产物索引）
  events.jsonl     # 追加事件（计划、启动、验收、返工、阻塞）
  artifacts/       # 汇总产物索引目录（软链或复制）
  final_summary.md # 结束时汇总
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.goal_run.models import GoalRunState

# ── 目录与文件名常量 ────────────────────────────────────────────────────

_GOAL_RUNS_DIR = ".veya-project/goal-runs"
_GOAL_MD = "GOAL.md"
_TASKGRAPH_JSON = "taskgraph.json"
_EVENTS_JSONL = "events.jsonl"
_FINAL_SUMMARY_MD = "final_summary.md"
_ARTIFACTS_DIR = "artifacts"


def _goal_run_dir(project_root: str, goal_id: str) -> Path:
    """返回 goal_run 目录的绝对路径。"""
    return Path(project_root) / _GOAL_RUNS_DIR / goal_id


def _ensure_goal_run_dir(project_root: str, goal_id: str) -> Path:
    """确保 goal_run 目录存在，返回路径。"""
    d = _goal_run_dir(project_root, goal_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / _ARTIFACTS_DIR).mkdir(exist_ok=True)
    return d


# ── 核心持久化函数 ──────────────────────────────────────────────────────

def save_goal_run(state: GoalRunState, project_root: str) -> None:
    """保存完整 goal_run 状态（taskgraph.json + GOAL.md + events.jsonl append）。"""
    run_dir = _ensure_goal_run_dir(project_root, state.goal_id)

    # 1. taskgraph.json（覆盖写）
    taskgraph_path = run_dir / _TASKGRAPH_JSON
    encoded_taskgraph = json.dumps(state.to_taskgraph_json(), ensure_ascii=False, indent=2)
    temporary_taskgraph = taskgraph_path.with_suffix(".json.tmp")
    with temporary_taskgraph.open("w", encoding="utf-8") as handle:
        handle.write(encoded_taskgraph)
        handle.flush()
        os.fsync(handle.fileno())
    temporary_taskgraph.replace(taskgraph_path)

    # 2. GOAL.md（首次写入，已存在则不覆盖）
    goal_md_path = run_dir / _GOAL_MD
    if not goal_md_path.exists():
        goal_md_path.write_text(
            f"# Goal: {state.goal_text}\n\n"
            f"## Interpretation\n{state.tasks[next(iter(state.tasks))].instruction if state.tasks else ''}\n\n"
            f"## Assumptions\n"
            + "\n".join(f"- {a}" for a in ["(no assumptions recorded)"])
            + f"\n\n## Task Graph\nGenerated at {datetime.now(UTC).isoformat()}",
            encoding="utf-8",
        )

    # 3. events.jsonl（append）
    # 调用方应在状态变更后调用 append_event，这里不再写入事件


def load_goal_run(project_root: str, goal_id: str) -> GoalRunState | None:
    """加载 goal_run 状态（从 taskgraph.json 反序列化）。"""
    run_dir = Path(project_root) / _GOAL_RUNS_DIR / goal_id
    taskgraph_path = run_dir / _TASKGRAPH_JSON

    if not taskgraph_path.exists():
        return None

    try:
        data = json.loads(taskgraph_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    # 从 taskgraph.json 恢复 GoalRunState
    # 需要原始 goal_text，尝试从 GOAL.md 读取
    goal_text = ""
    goal_md_path = run_dir / _GOAL_MD
    if goal_md_path.exists():
        content = goal_md_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            if line.startswith("# Goal:"):
                goal_text = line[len("# Goal:"):].strip()
                break

    state = GoalRunState.from_taskgraph_json(data, goal_text)
    return state


def append_event(project_root: str, goal_id: str, event: dict[str, Any]) -> None:
    """向 events.jsonl 追加事件（append-only）。"""
    run_dir = _ensure_goal_run_dir(project_root, goal_id)
    events_path = run_dir / _EVENTS_JSONL
    event.setdefault("timestamp", datetime.now(UTC).isoformat())
    with events_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    # GoalRun keeps its project-local audit file, while the runtime projection
    # receives the same fact in the canonical EventStore for cross-entry replay.
    try:
        from server.events import event_store

        raw_type = str(event.get("type") or event.get("event") or "")
        if raw_type in {"goal_started", "run_started", "understand_start"}:
            topic = "goal.started"
        elif raw_type in {"goal_completed", "run_completed", "completed"}:
            topic = "goal.completed"
        else:
            topic = "goal.updated"
        event_store.append(
            {
                "topic": topic,
                "trace_id": goal_id,
                "task_id": event.get("task_id"),
                "actor": str(event.get("actor") or "goal_run"),
                "payload": {"goal_id": goal_id, "goal_event": event},
            }
        )
    except Exception:
        pass


def write_final_summary(project_root: str, goal_id: str, summary: str, artifacts: list[str]) -> None:
    """写入 final_summary.md（G3 Finalize 阶段）。"""
    run_dir = _goal_run_dir(project_root, goal_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / _FINAL_SUMMARY_MD
    encoded = (
        f"# Final Summary\n\n{summary}\n\n## Artifacts\n"
        + "\n".join(f"- {a}" for a in artifacts)
        + f"\n\nCompleted at {datetime.now(UTC).isoformat()}"
    )
    temporary = summary_path.with_suffix(".md.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(summary_path)


def read_events(project_root: str, goal_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """读取 events.jsonl 尾部事件（用于轮询/诊断）。"""
    run_dir = Path(project_root) / _GOAL_RUNS_DIR / goal_id
    events_path = run_dir / _EVENTS_JSONL
    if not events_path.exists():
        return []
    lines = events_path.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    return events[-limit:]


def get_goal_run_artifacts(project_root: str, goal_id: str) -> list[str]:
    """返回该 goal_run 下的所有产物路径（从 taskgraph.json 聚合）。"""
    state = load_goal_run(project_root, goal_id)
    if not state:
        return []
    artifacts = []
    for tn in state.tasks.values():
        artifacts.extend(tn.artifacts)
    return artifacts
