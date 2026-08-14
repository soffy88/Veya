"""loop-plane domain.sched — 调度门面（SPEC §4.5 / §6.4）。

只做注册/触发门面；调度内核委托外部（现 server.automata 由装配方注入，
本服务不复制调度内核）。租户隔离与现 automata 规则一致。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

from app.domain.state.service import GoalService
from app.infra.event_store import new_id


class SchedService:
    """Job 注册/列表/删除/手动触发（持久化到 data_dir/jobs.json）。"""

    def __init__(self, goal_service: GoalService | None = None, *, jobs_path: Path | None = None) -> None:
        self._goals = goal_service
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._path = jobs_path or (Path.home() / ".veya" / "loop" / "jobs.json")
        self._load()
        # 调度内核委托点（装配方注入; 默认 None = 仅注册/手动触发）
        self._backend: Callable[[dict[str, Any]], None] | None = None

    def set_backend(self, backend: Callable[[dict[str, Any]], None]) -> None:
        """注入调度内核（现 automata 由 server 装配侧传入）。"""
        self._backend = backend

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._jobs = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._jobs = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._jobs, ensure_ascii=False, indent=2), encoding="utf-8")

    def register(self, name: str, *, cron: str = "", pattern: str = "", action: dict[str, Any] | None = None) -> dict[str, Any]:
        """注册 cron/pattern job。"""
        job_id = new_id("job_")
        job = {
            "id": job_id, "name": name, "cron": cron, "pattern": pattern,
            "action": action or {}, "tenant": "default",
        }
        with self._lock:
            self._jobs[job_id] = job
            self._save()
        if self._backend is not None:
            self._backend(job)
        return job

    def list_jobs(self) -> list[dict[str, Any]]:
        return list(self._jobs.values())

    def delete(self, job_id: str) -> bool:
        with self._lock:
            existed = self._jobs.pop(job_id, None) is not None
            if existed:
                self._save()
        return existed

    def trigger(self, job_id: str) -> dict[str, Any]:
        """手动触发：action 落到 GoalService（goal 创建/更新）或返回 job 详情。"""
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"job {job_id!r} 不存在")
        action = job.get("action") or {}
        action_type = action.get("type", "noop")
        if action_type == "create_goal" and self._goals is not None:
            goal = self._goals.create_goal(
                action.get("objective", job["name"]),
                action.get("todos", []),
            )
            return {"job_id": job_id, "triggered": True, "goal_id": goal["goal_id"]}
        return {"job_id": job_id, "triggered": True, "action": action}


__all__ = ["SchedService"]
