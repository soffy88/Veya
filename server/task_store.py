"""server.task_store — P1-03 Task Center 后端 (docs/VEYA_P1_P3_IMPLEMENTATION_SPEC.md §6)。

Task 记录的是「真实发生过的任务」——创建/状态变更/取消由调用方（chat 热路径、
审批链、GoalRun 等）显式写入，本模块只负责持久化与查询，**不做任何决策**。
符合 A-04：Task 状态是 Projection，不是主链控制器——本模块没有任何
``if task.status == ...: choose_executor(...)`` 之类的控制逻辑。

存储：单文件 JSON (沿用 _JsonRegistryStore 同款 tmp+replace 原子写)。
字段对齐规格 §6 Task 模型。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from server.acceptance import evaluate_acceptance, normalize_criteria
from server.events import EventStore

TaskStatus = Literal[
    "pending",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
]


@dataclass
class Task:
    """规格 §6 Task 模型，字段逐一对齐。"""

    id: str
    session_id: str
    title: str
    objective: str

    status: TaskStatus = "pending"
    workspace_id: str | None = None

    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    started_at: str | None = None
    completed_at: str | None = None

    current_step: str | None = None
    progress: float | None = None

    acceptance: list[Any] = field(default_factory=list)
    latest_checkpoint_id: str | None = None

    cost_usd: float = 0.0
    trace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_DEFAULT_PATH = str(Path.home() / ".veya" / "tasks.json")


class TaskProjection:
    """Rebuild the Task read model from persisted task lifecycle events."""

    _STATUS_BY_TOPIC: ClassVar[dict[str, str | None]] = {
        "task.created": "pending",
        "task.started": "running",
        "task.updated": None,
        "task.waiting_approval": "waiting_approval",
        "tool.approval_required": "waiting_approval",
        "tool.approved": "running",
        "task.completed": "completed",
        "task.failed": "failed",
        "task.cancelled": "cancelled",
        "checkpoint.created": None,
    }

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}

    def apply(self, event: dict[str, Any]) -> bool:
        topic = str(event.get("topic") or event.get("type") or event.get("event") or "")
        if not topic.startswith("task.") and topic not in {
            "tool.approval_required",
            "tool.approved",
            "checkpoint.created",
        }:
            return False

        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        task_data = payload.get("task")
        task_id = event.get("task_id") or payload.get("task_id")
        if isinstance(task_data, dict):
            task_id = task_data.get("id") or task_id

        if not task_id:
            return False

        current = dict(self.tasks.get(str(task_id), {}))
        if isinstance(task_data, dict):
            current.update(task_data)
        status = self._STATUS_BY_TOPIC.get(topic)
        if status is not None:
            current["status"] = status
        current["id"] = str(task_id)
        self.tasks[str(task_id)] = current
        return True

    @classmethod
    def from_events(cls, events: list[dict[str, Any]]) -> TaskProjection:
        projection = cls()
        for event in events:
            projection.apply(event)
        return projection

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {task_id: dict(task) for task_id, task in self.tasks.items()}


class TaskStore:
    """Task 持久化 + 查询。线程安全；进程内单例由调用方持有。"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        event_store: EventStore | None = None,
        event_path: str | Path | None = None,
    ):
        self.path = Path(
            path or os.environ.get("VEYA_TASK_STORE_PATH", _DEFAULT_PATH)
        ).expanduser()
        self._lock = threading.RLock()
        if event_store is not None:
            self.event_store = event_store
        elif event_path is not None:
            self.event_store = EventStore(event_path)
        elif path is not None:
            self.event_store = EventStore(self.path.with_suffix(".events.jsonl"))
        else:
            self.event_store = EventStore()
        self._tasks: dict[str, dict[str, Any]] = self._load()
        # Event history is authoritative.  The JSON file is only a read-model
        # cache and may be stale after a crash or another process appended
        # lifecycle events.
        self._rebuild_from_events(save=True)

    def _load(self) -> dict[str, dict[str, Any]]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._tasks, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.path)

    def _append_task_event(
        self,
        topic: str,
        task: Task,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"task": task.to_dict()}
        if extra:
            payload.update(extra)
        self.event_store.append(
            {
                "topic": topic,
                "trace_id": task.trace_id or task.id,
                "session_id": task.session_id,
                "task_id": task.id,
                "actor": "system",
                "payload": payload,
            }
        )

    def _rebuild_from_events(self, *, save: bool) -> None:
        projection = TaskProjection.from_events(self.event_store.read_all())
        if not projection.tasks:
            return
        with self._lock:
            self._tasks = projection.snapshot()
            if save:
                self._save()

    # ── 写操作 ──────────────────────────────────────────────────────
    def create(
        self,
        *,
        session_id: str,
        title: str,
        objective: str,
        workspace_id: str | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
        acceptance: list[dict[str, Any]] | None = None,
    ) -> Task:
        task = Task(
            id=task_id or f"task_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            title=title or objective[:40],
            objective=objective,
            workspace_id=workspace_id,
            status="pending",
            trace_id=trace_id,
            acceptance=normalize_criteria(acceptance),
        )
        with self._lock:
            self._append_task_event("task.created", task)
            self._tasks[task.id] = task.to_dict()
            self._save()
        return task

    def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        current_step: str | None = None,
        progress: float | None = None,
    ) -> Task | None:
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                return None
            next_rec = dict(rec)
            previous_status = next_rec.get("status")
            next_rec["status"] = status
            next_rec["updated_at"] = datetime.now(UTC).isoformat()
            if status == "running" and rec.get("started_at") is None:
                next_rec["started_at"] = datetime.now(UTC).isoformat()
            if status in ("completed", "failed", "cancelled"):
                next_rec["completed_at"] = datetime.now(UTC).isoformat()
            if current_step is not None:
                next_rec["current_step"] = current_step
            if progress is not None:
                next_rec["progress"] = max(0.0, min(1.0, float(progress)))
            task = Task(**next_rec)
            topic = {
                "running": "task.started",
                "waiting_approval": "task.waiting_approval",
                "completed": "task.completed",
                "failed": "task.failed",
                "cancelled": "task.cancelled",
            }.get(status, "task.updated")
            self._append_task_event(
                topic,
                task,
                extra={"previous_status": previous_status},
            )
            self._tasks[task_id] = task.to_dict()
            self._save()
            return task

    def set_cost(self, task_id: str, cost_usd: float) -> Task | None:
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                return None
            next_rec = dict(rec)
            next_rec["cost_usd"] = float(cost_usd or 0.0)
            next_rec["updated_at"] = datetime.now(UTC).isoformat()
            task = Task(**next_rec)
            self._append_task_event("task.updated", task)
            self._tasks[task_id] = task.to_dict()
            self._save()
            return task

    # ── 查询 ────────────────────────────────────────────────────────
    def get(self, task_id: str) -> Task | None:
        rec = self._tasks.get(task_id)
        return Task(**rec) if rec else None

    def list(
        self,
        *,
        workspace_id: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 100,
    ) -> list[Task]:
        items = [Task(**r) for r in self._tasks.values()]
        if workspace_id:
            items = [t for t in items if t.workspace_id == workspace_id]
        if status:
            items = [t for t in items if t.status == status]
        if session_id:
            items = [t for t in items if t.session_id == session_id]
        if date_from:
            items = [t for t in items if t.created_at >= date_from]
        if date_to:
            items = [t for t in items if t.created_at <= date_to]
        items.sort(key=lambda t: t.created_at, reverse=True)
        return items[: max(1, min(int(limit), 500))]

    def cancel(self, task_id: str, *, reason: str = "") -> Task | None:
        current = self.get(task_id)
        if current is None or current.status in {"completed", "failed", "cancelled"}:
            return current
        task = self.update_status(task_id, "cancelled")
        if task is not None and reason:
            # reason 记录在 objective 后缀（保持 schema 不变，不新增字段）
            pass
        return task

    def events(self, task_id: str) -> list[dict[str, Any]]:
        return self.event_store.read_all(task_id=task_id)

    def set_checkpoint(
        self, task_id: str, checkpoint_id: str, *, stage: str | None = None
    ) -> Task | None:
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                return None
            next_rec = dict(rec)
            next_rec["latest_checkpoint_id"] = checkpoint_id
            next_rec["updated_at"] = datetime.now(UTC).isoformat()
            task = Task(**next_rec)
            self._append_task_event(
                "checkpoint.created",
                task,
                extra={
                    "checkpoint_id": checkpoint_id,
                    **({"stage": stage} if stage else {}),
                },
            )
            self._tasks[task_id] = task.to_dict()
            self._save()
            return task

    def evaluate_acceptance(
        self,
        task_id: str,
        *,
        workspace: str | Path = ".",
        timeout_s: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        """Run deterministic acceptance checks and persist their evidence."""
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                return None
            results = evaluate_acceptance(
                rec.get("acceptance") or [], workspace=workspace, timeout_s=timeout_s
            )
            next_rec = dict(rec)
            next_rec["acceptance"] = results
            next_rec["updated_at"] = datetime.now(UTC).isoformat()
            task = Task(**next_rec)
            self._append_task_event(
                "task.updated",
                task,
                extra={"acceptance_results": results},
            )
            self._tasks[task_id] = task.to_dict()
            self._save()
            return results

    def rebuild_from_events(self) -> list[Task]:
        """Rebuild and persist the task read model from its event stream."""
        self._rebuild_from_events(save=True)
        return self.list()


# 模块级单例（server 复用；测试可注入独立实例）
task_store = TaskStore()
