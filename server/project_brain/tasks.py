# 3O-IO-ALLOW — server/project_brain/tasks.py: tasks.json projection and persistence in .veya-project/
"""W16 Project Brain — ProjectTask model, store, and markdown renderer.

Spec §3:
  ProjectTask tasks.json[task_id]:
    objective: str
    goal_run_id: str = ""
    binding_id: str = "" (W12 SupervisorBinding)
    routine_id: str = ""
    status: str (accepted | running | verifying | done | failed | cancelled)
    receipts: dict[str, dict[str, bool]] (message_id -> {receipt_sent: bool, conclusion_sent: bool})
    last_report_seq: int = 0
    verifier: dict[str, Any] | None (latest VerifierEvidence snapshot)
    fix_notes: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.bot_team.conversations import atomic_write_json
from server.project_store import ProjectStore

_TASKS_JSON = "tasks.json"

TASK_STATUS_ACCEPTED = "accepted"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_VERIFYING = "verifying"
TASK_STATUS_DONE = "done"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"

VALID_STATUSES = {
    TASK_STATUS_ACCEPTED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_VERIFYING,
    TASK_STATUS_DONE,
    TASK_STATUS_FAILED,
    TASK_STATUS_CANCELLED,
}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ProjectTask:
    """A project-level task indexed in tasks.json."""

    task_id: str
    objective: str
    goal_run_id: str = ""
    binding_id: str = ""
    routine_id: str = ""
    status: str = TASK_STATUS_ACCEPTED
    receipts: dict[str, dict[str, bool]] = field(default_factory=dict)
    last_report_seq: int = 0
    verifier: dict[str, Any] | None = None
    fix_notes: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            self.status = TASK_STATUS_ACCEPTED
        now = _now_iso()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "goal_run_id": self.goal_run_id,
            "binding_id": self.binding_id,
            "routine_id": self.routine_id,
            "status": self.status,
            "receipts": dict(self.receipts),
            "last_report_seq": self.last_report_seq,
            "verifier": self.verifier,
            "fix_notes": list(self.fix_notes),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectTask:
        status = str(data.get("status") or TASK_STATUS_ACCEPTED).strip().lower()
        if status not in VALID_STATUSES:
            status = TASK_STATUS_ACCEPTED
        raw_receipts = data.get("receipts") or {}
        receipts = {
            str(k): {str(rk): bool(rv) for rk, rv in v.items()}
            for k, v in raw_receipts.items()
            if isinstance(v, dict)
        }
        return cls(
            task_id=str(data.get("task_id") or ""),
            objective=str(data.get("objective") or ""),
            goal_run_id=str(data.get("goal_run_id") or ""),
            binding_id=str(data.get("binding_id") or ""),
            routine_id=str(data.get("routine_id") or ""),
            status=status,
            receipts=receipts,
            last_report_seq=int(data.get("last_report_seq") or 0),
            verifier=data.get("verifier"),
            fix_notes=[str(x) for x in (data.get("fix_notes") or [])],
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


def _tasks_path(project_root: str | Path) -> Path:
    store = ProjectStore(project_root)
    store.ensure_layout()
    return store.dir / _TASKS_JSON


def load_tasks(project_root: str | Path) -> dict[str, ProjectTask]:
    """Load tasks from .veya-project/tasks.json safely."""
    path = _tasks_path(project_root)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        if isinstance(raw, dict) and "tasks" in raw and isinstance(raw["tasks"], dict):
            raw_tasks = raw["tasks"]
        elif isinstance(raw, dict):
            raw_tasks = raw
        else:
            return {}
        tasks: dict[str, ProjectTask] = {}
        for tid, tdict in raw_tasks.items():
            if isinstance(tdict, dict):
                t = ProjectTask.from_dict(tdict)
                tasks[t.task_id or tid] = t
        return tasks
    except Exception:
        return {}


def save_tasks(project_root: str | Path, tasks: dict[str, ProjectTask]) -> None:
    """Save tasks to .veya-project/tasks.json atomically."""
    path = _tasks_path(project_root)
    payload = {
        "tasks": {tid: t.to_dict() for tid, t in tasks.items()},
        "updated_at": _now_iso(),
    }
    atomic_write_json(path, payload)


def get_task(project_root: str | Path, task_id: str) -> ProjectTask | None:
    """Get a task by task_id."""
    tasks = load_tasks(project_root)
    return tasks.get(task_id)


def upsert_task(project_root: str | Path, task: ProjectTask) -> ProjectTask:
    """Insert or update a task, updating updated_at timestamp."""
    task.updated_at = _now_iso()
    tasks = load_tasks(project_root)
    tasks[task.task_id] = task
    save_tasks(project_root, tasks)
    return task


def list_tasks(project_root: str | Path) -> list[ProjectTask]:
    """List all project tasks ordered by creation time descending."""
    tasks = load_tasks(project_root)
    return sorted(tasks.values(), key=lambda t: t.created_at, reverse=True)


def record_receipt(
    project_root: str | Path,
    task_id: str,
    message_id: str,
    *,
    receipt_sent: bool = True,
    conclusion_sent: bool = False,
) -> ProjectTask | None:
    """Record receipt or conclusion status for a message id."""
    task = get_task(project_root, task_id)
    if not task:
        return None
    msg_entry = task.receipts.setdefault(
        message_id, {"receipt_sent": False, "conclusion_sent": False}
    )
    if receipt_sent:
        msg_entry["receipt_sent"] = True
    if conclusion_sent:
        msg_entry["conclusion_sent"] = True
    return upsert_task(project_root, task)


def is_conclusion_sent(task: ProjectTask, message_id: str) -> bool:
    """Check if conclusion was already sent for this message_id."""
    return bool(task.receipts.get(message_id, {}).get("conclusion_sent", False))


def mark_conclusion_sent(project_root: str | Path, task_id: str, message_id: str) -> bool:
    """Mark conclusion as sent if not already sent.

    Returns:
      True if marked (caller should deliver conclusion).
      False if already sent (caller should suppress duplicate conclusion).
    """
    task = get_task(project_root, task_id)
    if not task:
        return False
    if is_conclusion_sent(task, message_id):
        return False
    msg_entry = task.receipts.setdefault(
        message_id, {"receipt_sent": True, "conclusion_sent": False}
    )
    msg_entry["conclusion_sent"] = True
    upsert_task(project_root, task)
    return True


def render_tasks_markdown(tasks: list[ProjectTask]) -> str:
    """Render active tasks and blockers for Section 4 of Brief."""
    active = [
        t
        for t in tasks
        if t.status in (TASK_STATUS_ACCEPTED, TASK_STATUS_RUNNING, TASK_STATUS_VERIFYING)
    ]
    blocked = [t for t in tasks if t.status == TASK_STATUS_FAILED]

    if not active and not blocked:
        return "暂无进行中任务。"

    lines: list[str] = []
    if active:
        lines.append("### 进行中")
        for t in active:
            lines.append(f"- [{t.status}] {t.objective} (ID: {t.task_id})")
    if blocked:
        lines.append("### 阻塞 / 失败")
        for t in blocked:
            notes = f" — {'; '.join(t.fix_notes)}" if t.fix_notes else ""
            lines.append(f"- [failed] {t.objective} (ID: {t.task_id}){notes}")

    return "\n".join(lines)


def sync_tasks_to_brief(project_root: str | Path) -> None:
    """Sync current task state into Section 4 of PROJECT_STATE.md."""
    from server.project_brain.brief import update_brief_section

    tasks = list_tasks(project_root)
    rendered = render_tasks_markdown(tasks)
    update_brief_section(project_root, "active_tasks", rendered)
