"""hicode_task_queue — Hicode 后台任务队列 (并发提交 / 串行执行 / 可停止 / 断线不丢)。

serve (hicode oservi) 是单活跃会话 (单 controller): 同一时刻只能跑一个 turn。
本队列在 veya 层提供:
  - 并发提交: 多个编程任务入队互不阻塞, 立即返回 task id;
  - 串行执行: 单个 worker 依次消费 (serve 单会话限制, 安全无文件冲突);
  - 停止: running → POST /cancel 真正中断 serve turn (不是只断 SSE);
          queued → 直接置 cancelled 不消费;
  - 断线不丢: worker 是独立 asyncio.Task, 不随 SSE 会话取消而中断,
              结果留在队列, 可随时查询/续做。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("hicode.queue")


@dataclass
class TaskRecord:
    id: str
    spec: str
    status: str = "queued"  # queued → running → done | failed | cancelled
    workspace: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    error: str = ""
    events: list[dict] = field(default_factory=list)  # 进度事件快照 (供查询)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_requested: bool = False
    _done: asyncio.Event = field(default_factory=asyncio.Event)
    _watchers: set = field(default_factory=set)  # wait() 实时进度订阅者

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "summary": self.summary,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "cancel_requested": self.cancel_requested,
        }


class HicodeTaskQueue:
    """全局任务队列 (单例 hicode_task_queue)。"""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._ready: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._max_concurrent = 1  # serve 单活跃会话 → 串行执行

    # ── 提交 / 等待 ────────────────────────────────────────────────
    async def submit(self, spec: str, *, workspace: str | None = None,
                     meta: dict[str, Any] | None = None) -> str:
        """入队一个编程任务, 立即返回 task id。"""
        tid = uuid.uuid4().hex[:10]
        rec = TaskRecord(id=tid, spec=spec, workspace=workspace,
                         meta=meta or {})
        self._tasks[tid] = rec
        await self._ready.put(tid)
        self._ensure_worker()
        logger.info("hicode 队列: 提交 %s (queued, 队列深度=%d)",
                    tid, self._ready.qsize() + 1)
        return tid

    async def wait(self, tid: str,
                   on_progress: Callable[[dict], None] | None = None) -> TaskRecord:
        """等待任务完成 (done/failed/cancelled)。on_progress 收到进度事件。

        注意: 调用方被取消 (如 SSE 断线) 时本协程抛 CancelledError, 但
        worker 是独立任务 → 任务继续后台执行, 结果留在队列。
        """
        rec = self._tasks.get(tid)
        if rec is None:
            raise KeyError(f"task {tid} not found")
        if on_progress is not None:
            # 已发生的进度先补发, 之后 worker 每产生新事件实时推给订阅者
            for ev in rec.events:
                on_progress(ev)
            rec._watchers.add(on_progress)
            try:
                await rec._done.wait()
            finally:
                rec._watchers.discard(on_progress)
        else:
            await rec._done.wait()
        return rec

    def get(self, tid: str) -> TaskRecord | None:
        return self._tasks.get(tid)

    def list(self, limit: int = 12) -> list[dict]:
        """按创建时间倒序返回任务快照 (最近 limit 条)。"""
        recs = sorted(self._tasks.values(),
                      key=lambda r: r.created_at, reverse=True)
        return [r.snapshot() for r in recs[:limit]]

    # ── 停止 ───────────────────────────────────────────────────────
    async def stop(self, tid: str, reason: str = "user stop") -> bool:
        """停止任务: running → serve POST /cancel (真正中断 turn);
        queued → 直接置 cancelled。"""
        rec = self._tasks.get(tid)
        if rec is None:
            return False
        if rec.status == "queued":
            rec.status = "cancelled"
            rec.error = reason
            rec.updated_at = time.time()
            rec._done.set()
            logger.info("hicode 队列: 取消排队任务 %s", tid)
            return True
        if rec.status == "running":
            rec.cancel_requested = True
            rec.updated_at = time.time()
            # 1) 软中断: serve POST /cancel (秒级, 但模型调用可能不响应)
            from server.hicode_serve import get_serve_client

            client = get_serve_client()
            try:
                await client.cancel()
                logger.info("hicode 队列: 已请求 serve cancel → %s", tid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hicode 队列: serve cancel 失败 %s: %s", tid, exc)
            # 2) 软停观察窗口: 12s 内 turn 未中断 → 硬重启 serve (真正停止)
            try:
                await asyncio.wait_for(rec._done.wait(), timeout=12)
            except asyncio.TimeoutError:
                logger.warning(
                    "hicode 队列: cancel 未中断 %s → 硬重启 serve", tid)
                try:
                    if not await client.restart_serve():
                        logger.warning("hicode 队列: serve 重启未恢复健康")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("hicode 队列: serve 硬重启失败 %s: %s",
                                   tid, exc)
                # 等 worker 收尾 (events 断开 → run_task 返回)
                try:
                    await asyncio.wait_for(rec._done.wait(), timeout=30)
                except asyncio.TimeoutError:
                    logger.warning("hicode 队列: 任务 %s 硬停后仍未收尾", tid)
            return True
        return False

    # ── worker ─────────────────────────────────────────────────────
    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop())

            def _on_done(t: asyncio.Task) -> None:
                if t.exception() and not isinstance(
                        t.exception(), asyncio.CancelledError):
                    logger.warning("hicode worker 退出: %s", t.exception())

            self._worker.add_done_callback(_on_done)

    async def _worker_loop(self) -> None:
        while True:
            try:
                tid = await self._ready.get()
            except asyncio.CancelledError:
                raise  # loop 关闭 (asyncio.run 清理) — 静默
            rec = self._tasks.get(tid)
            if rec is None or rec.status == "cancelled":
                continue
            rec.status = "running"
            rec.updated_at = time.time()
            try:
                await self._run_one(rec)
            except Exception as exc:  # noqa: BLE001
                logger.exception("hicode 任务 %s 异常", tid)
                rec.status = "failed"
                rec.error = str(exc)[:400]
            finally:
                rec.updated_at = time.time()
                rec._done.set()

    async def _run_one(self, rec: TaskRecord) -> None:
        # 必须调内核 (不经过队列的工具入口), 否则递归入队死循环
        from server.hicode_agent import _execute_hicode_core

        def _push(ev: dict) -> None:
            rec.events.append(ev)
            if len(rec.events) > 200:  # 事件快照限长
                rec.events.pop(0)
            # 实时推给所有 wait() 订阅者 (SSE 断线/回调异常绝不拖垮 worker)
            for w in list(rec._watchers):
                try:
                    w(ev)
                except Exception:  # noqa: BLE001
                    pass

        try:
            summary = await _execute_hicode_core(
                rec.spec,
                workspace=rec.workspace,
                timeout_sec=int(rec.meta.get("timeout_sec") or 900),
                on_event=_push,
            )
        except asyncio.CancelledError:
            raise
        # 执行期间被用户停止 → 结果按 cancelled 记 (serve turn 已被 /cancel 打断)
        if rec.cancel_requested:
            rec.status = "cancelled"
            rec.error = "user stop"
            rec.summary = summary[:400] if summary else ""
        elif summary.startswith("错误") or summary.startswith("hicode 不可用"):
            rec.status = "failed"
            rec.error = summary[:400]
            rec.summary = summary
        else:
            rec.status = "done"
            rec.summary = summary


# 模块级单例 (server 复用)
hicode_task_queue = HicodeTaskQueue()
