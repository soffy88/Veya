"""Veya Automata: 后台自动化守护进程(薄适配层)。

3O 单一来源 (§1.4): 调度器本体已固化为主库 omodul.automata.AutomataScheduler。
本层保留既有 API(VeyaAutomata / get_automata / reset_automata / 默认无头执行器)。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from veya.platform import omodul as _load_omodul

_omodul = _load_omodul()


class VeyaAutomata:
    """后台自动化守护进程(委托主库 omodul.automata 调度器)。"""

    def __init__(
        self,
        execute_callback: Callable[[str], Awaitable[str]],
        *,
        jobs_db_path: str | Path | None = None,
        restore_on_start: bool = True,
    ):
        self._scheduler = _omodul.automata.AutomataScheduler(
            execute_callback=execute_callback,
            jobs_db_path=jobs_db_path,
            restore_on_start=restore_on_start,
        )

    @property
    def scheduler(self) -> Any:
        return self._scheduler.scheduler

    @property
    def jobs_db_path(self) -> Path:
        return self._scheduler.jobs_db_path

    def register_cron_task(self, cron_expr: str, task_prompt: str, task_id: str | None = None) -> str:
        return self._scheduler.register_cron_task(cron_expr, task_prompt, task_id=task_id)

    def remove_task(self, task_id: str) -> str:
        return self._scheduler.remove_task(task_id)

    def get_jobs(self) -> list[dict]:
        return self._scheduler.get_jobs()

    def trigger_event(self, event_name: str, payload: dict) -> str:
        return self._scheduler.trigger_event(event_name, payload)

    async def _run_headless_mission(self, trigger_context: str, task_prompt: str) -> str:
        return await self._scheduler._run_headless_mission(trigger_context, task_prompt)

    def get_recent_results(self, limit: int = 10) -> list[dict]:
        return self._scheduler.get_recent_results(limit)

    def get_status(self) -> dict:
        return self._scheduler.get_status()

    def shutdown(self) -> None:
        self._scheduler.shutdown()


# =========================================================================
# 模块级惰性单例(server 复用; 测试注入独立实例)
# =========================================================================

_automata: VeyaAutomata | None = None


def _default_headless_runner() -> Callable[[str], Awaitable[str]]:
    """默认无头执行器: 构造一个主脑实例去静默执行合成 Prompt。"""

    async def _runner(synthetic_prompt: str) -> str:
        # 延迟 import 避免循环依赖(automata → coordinator_master → automata 单例)
        from server.coordinator_master import MasterCoordinator

        coordinator = MasterCoordinator()  # 用 .env / 环境变量的后台专属 Key
        result = await coordinator.chat_stream(synthetic_prompt, session_id=f"auto_{uuid.uuid4().hex[:8]}")
        if result.get("status") == "success":
            return result.get("final_answer", "") or "(no answer)"
        return f"HEADLESS FAILED: {result.get('error', 'unknown')}"

    return _runner


def get_automata(execute_callback: Callable[[str], Awaitable[str]] | None = None) -> VeyaAutomata:
    """惰性全局单例: 首次调用创建并启动守护进程。

    若既有实例的调度器已停止(如宿主 event loop 关闭), 自动重建。
    """
    global _automata
    if _automata is None or not _automata.scheduler.running:
        _automata = VeyaAutomata(execute_callback=execute_callback or _default_headless_runner())
    return _automata


def reset_automata() -> None:
    """测试用: 重置全局单例。"""
    global _automata
    if _automata is not None:
        _automata.shutdown()
    _automata = None
