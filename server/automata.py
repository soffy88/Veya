"""Veya Automata: 后台自动化守护进程(薄适配层)。

3O 单一来源 (§1.4): 调度器本体已固化为主库 omodul.automata.AutomataScheduler。
本层保留既有 API(VeyaAutomata / get_automata / reset_automata / 默认无头执行器)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from veya.platform import omodul as _load_omodul

_omodul = _load_omodul()

logger = logging.getLogger("automata")


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
        # 网格搜索后台流水线 (Fire-and-Forget 工单看管)
        self._grid_tasks: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    # =========================================================================
    # 网格搜索工单 (Fire-and-Forget 异步分发)
    # =========================================================================

    def start_grid_search_task(
        self,
        asset_id: str,
        strategy_code: str,
        param_grid: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> str:
        """主脑异步脱壳: 生成工单扔给 Automata 看管, 立即返回, 绝不挂起主脑。

        Returns:
            task_id (形如 ``grid_search_xxxxxxxx``).

        后台流水线 (Automata 守护进程):
            ProcessPool 进度拦截 → 物理层 Reduce → 无头主脑 Agentic Reduce → 通知。
        """
        task_id = f"grid_search_{uuid.uuid4().hex[:8]}"
        self._loop = asyncio.get_running_loop()
        task = asyncio.create_task(
            self._run_grid_search_pipeline(task_id, asset_id, strategy_code, param_grid, session_id)
        )
        # 强引用: asyncio 只对裸 create_task 持弱引用, 防中途被 GC (同 chat_coordinator 模式)
        self._grid_tasks.add(task)
        task.add_done_callback(self._grid_tasks.discard)
        logger.info(
            "[automata] grid search task submitted: %s (%d combos)", task_id, len(param_grid)
        )
        return task_id

    async def _run_grid_search_pipeline(
        self,
        task_id: str,
        asset_id: str,
        strategy_code: str,
        param_grid: dict[str, Any],
        session_id: str | None,
    ) -> None:
        """后台流水线: 进度拦截 → 并发回测 → 物理 Reduce → 无头唤醒合成 → 通知。"""
        from server.notification_center import global_notifier
        from server.quant_coprocessor import quant_coprocessor
        from veya.platform import oprim as _load_oprim

        _oprim = _load_oprim()
        loop = asyncio.get_running_loop()

        # 1. 进度拦截: ProcessPool 编排线程 → 跳回事件循环 → 悬浮窗 SSE
        def on_progress(done: int, total: int, latest: dict[str, Any]) -> None:
            detail = (
                f"最新 Sharpe: {latest['sharpe']:.2f}"
                if "sharpe" in latest
                else f"失败: {str(latest.get('error', ''))[:100]}"
            )

            async def _push() -> None:
                global_notifier.push(
                    "INFO",
                    f"网格搜索进行中 ({done}/{total})",
                    detail,
                    {"task_id": task_id, "session_id": session_id, "done": done, "total": total},
                )

            with contextlib.suppress(RuntimeError):  # 宿主 loop 已关闭
                asyncio.run_coroutine_threadsafe(_push(), loop)

        # 2. 跑进程池 (execute_grid_search 内部已 run_in_executor, 事件循环不被阻塞)
        try:
            results = await quant_coprocessor.execute_grid_search(
                strategy_code, asset_id, param_grid, progress_callback=on_progress
            )
        except Exception as exc:
            logger.warning("[automata] grid search %s failed: %s", task_id, exc)
            global_notifier.push(
                "ERROR", "网格搜索崩溃", str(exc), {"task_id": task_id, "session_id": session_id}
            )
            return

        # 3. 物理层规约 (Reduce): 夏普最高者
        best = _oprim.reduce_best(results)
        if best is None:
            global_notifier.push(
                "ERROR",
                "网格搜索失败",
                "所有参数组合均报错",
                {"task_id": task_id, "session_id": session_id},
            )
            return

        # 4. 无头唤醒 (Agentic Reduce): 主脑写总结 + 生成 ECharts 热力图 artifact
        heatmap_keys = list(param_grid.keys())
        heatmap = (
            _oprim.build_heatmap_payload(results, heatmap_keys[0], heatmap_keys[1])
            if len(heatmap_keys) >= 2
            else {}
        )
        synthesis_prompt = (
            "[SYSTEM TRIGGER] The backend grid search for "
            f"{asset_id} has completed. Task: {task_id}.\n"
            f"Search space: {param_grid}\n"
            f"Total combinations tested: {len(results)}\n"
            f"Best Result: Parameters {best['params']} yielded a Sharpe of {best['sharpe']:.2f}.\n"
            f"All results: {json.dumps(results, ensure_ascii=False)}\n\n"
            "YOUR TASK: Generate a concise summary of this backtest for the user. Then output "
            'a <veya-artifact type="react"> containing an ECharts heatmap/bar chart to visualize '
            "how the parameters affected the Sharpe ratio."
        )
        try:
            summary = await self._scheduler.execute_callback(synthesis_prompt)
        except Exception as exc:  # pragma: no cover - 无头合成失败不吞掉最终结果
            logger.warning("[automata] headless synthesis failed: %s", exc)
            summary = f"(无头合成失败: {exc})"

        # 5. 终极交付: 悬浮窗弹窗 + artifact 载荷
        global_notifier.push(
            "SUCCESS",
            "🎯 网格搜索完成",
            f"最优参数 {best['params']}，Sharpe: {best['sharpe']:.2f}",
            {
                "task_id": task_id,
                "session_id": session_id,
                "best": best,
                "heatmap": heatmap,
                "content": summary,
            },
        )

    @property
    def scheduler(self) -> Any:
        return self._scheduler.scheduler

    @property
    def jobs_db_path(self) -> Path:
        return self._scheduler.jobs_db_path

    def register_cron_task(
        self, cron_expr: str, task_prompt: str, task_id: str | None = None
    ) -> str:
        # 多用户隔离: 注册时记录归属用户 (工具执行 task 内 auth contextvar 有效)
        from server.auth import current_user

        user_id = current_user()["user_id"] or ""
        return self._scheduler.register_cron_task(
            cron_expr, task_prompt, task_id=task_id, user_id=user_id
        )

    def remove_task(self, task_id: str) -> str:
        # 多用户隔离: 只能删除自己的 cron 任务
        from server.auth import current_user

        uid = current_user()["user_id"] or ""
        target = next((j for j in self._scheduler.get_jobs() if j.get("id") == task_id), None)
        if target and str(task_id).startswith("cron_"):
            owner = target.get("user_id", "")
            if owner and owner != uid:
                return f"无权删除任务: {task_id} (属于其他用户)"
        return self._scheduler.remove_task(task_id)

    def get_jobs(self) -> list[dict]:
        # 多用户隔离: 只列当前用户的 cron 任务 (匿名用户看无主任务)
        from server.auth import current_user

        uid = current_user()["user_id"] or ""
        jobs = self._scheduler.get_jobs()
        return [
            j
            for j in jobs
            if not str(j.get("id", "")).startswith("cron_") or j.get("user_id", "") in ("", uid)
        ]

    def trigger_event(self, event_name: str, payload: dict) -> str:
        return self._scheduler.trigger_event(event_name, payload)

    async def _run_headless_mission(
        self, trigger_context: str, task_prompt: str, user_id: str = ""
    ) -> str:
        return await self._scheduler._run_headless_mission(trigger_context, task_prompt, user_id)

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

    async def _runner(synthetic_prompt: str, user_id: str = "") -> str:
        # 延迟 import 避免循环依赖(automata → coordinator_master → automata 单例)
        from server import auth as auth_mod
        from server.coordinator_master import MasterCoordinator

        if user_id:
            # 触发时注入归属用户 → 无头 agent 的会话/计划/通知按该用户隔离
            auth_mod.set_user({"user_id": user_id, "username": user_id[:12]})
        coordinator = MasterCoordinator()  # 用 .env / 环境变量的后台专属 Key
        result = await coordinator.chat_stream(
            synthetic_prompt, session_id=f"auto_{uuid.uuid4().hex[:8]}"
        )
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
