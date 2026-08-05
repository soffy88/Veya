"""Veya State Engine: 任务状态机、持久化与时间旅行断点续传(薄适配层)。

3O 单一来源 (§1.4): 本体已固化为主库 omodul.task_manager.TaskManager
(组合 oprim._task_state 纯状态机 + obase.task_store SQLite 事务存储)。
本层保留脚手架 API: VeyaTaskManager = TaskManager。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from veya.platform import obase as _load_obase
from veya.platform import omodul as _load_omodul

_obase = _load_obase()
_omodul = _load_omodul()


class VeyaTaskManager:
    """任务状态机管理器(SQLite 持久化 + 断点续传 + Time-Travel 回滚)。"""

    def __init__(self, db_path: str = "~/.veya/tasks.db"):
        db = Path(db_path).expanduser() if db_path else None
        store = _obase.task_store.TaskStore(db_path=db or "~/.veya/tasks.db")
        self._manager = _omodul.task_manager.TaskManager(store=store)

    def create_task(self, task_id: str, total_steps: int, initial_payload: dict | None = None) -> str:
        return self._manager.create_task(task_id, total_steps, initial_payload)

    def checkpoint(
        self,
        task_id: str,
        step_index: int,
        step_payload: dict | None = None,
        status: str = "RUNNING",
    ) -> dict[str, Any]:
        return self._manager.checkpoint(task_id, step_index, step_payload, status=status)

    def get_resume_context(self, task_id: str) -> dict[str, Any]:
        return self._manager.get_resume_context(task_id)

    def rollback(self, task_id: str, to_step: int) -> dict[str, Any]:
        return self._manager.rollback(task_id, to_step)

    def complete(self, task_id: str) -> None:
        self._manager.complete(task_id)

    def fail(self, task_id: str) -> None:
        self._manager.fail(task_id)

    def list_tasks(self) -> list[dict[str, Any]]:
        return self._manager.list_tasks()
