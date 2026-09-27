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
import contextlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.goal_run.leaf import LeafResult
from server.goal_run.store import save_goal_run
from veya.oprim.fs import fs_delete

logger = logging.getLogger("hicode.queue")

_HICODE_GOAL_ENVELOPE_KIND = "veya.hicode.goal"
_HICODE_GOAL_ENVELOPE_VERSION = 1
_HICODE_PERSISTED_META = (
    "timeout_sec",
    "max_steps",
    "session_id",
    "continue_",
    "force_cli",
    "sid",
)


def _encode_goal_instruction(spec: str, meta: dict[str, Any]) -> str:
    persisted = {key: meta.get(key) for key in _HICODE_PERSISTED_META if key in meta}
    return json.dumps(
        {
            "kind": _HICODE_GOAL_ENVELOPE_KIND,
            "version": _HICODE_GOAL_ENVELOPE_VERSION,
            "spec": spec,
            "meta": persisted,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _decode_goal_instruction(value: str) -> tuple[str, dict[str, Any]]:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value, {}
    if not isinstance(payload, dict) or payload.get("kind") != _HICODE_GOAL_ENVELOPE_KIND:
        return value, {}
    if int(payload.get("version") or 0) != _HICODE_GOAL_ENVELOPE_VERSION:
        return value, {}
    spec = payload.get("spec")
    meta = payload.get("meta")
    if not isinstance(spec, str) or not isinstance(meta, dict):
        return value, {}
    return spec, {key: meta.get(key) for key in _HICODE_PERSISTED_META if key in meta}


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
    async def submit(
        self, spec: str, *, workspace: str | None = None, meta: dict[str, Any] | None = None
    ) -> str:
        """入队一个编程任务, 立即返回 task id。"""
        tid = uuid.uuid4().hex[:10]
        rec = TaskRecord(id=tid, spec=spec, workspace=workspace, meta=meta or {})
        self._tasks[tid] = rec
        await self._ready.put(tid)
        self._ensure_worker()
        logger.info("hicode 队列: 提交 %s (queued, 队列深度=%d)", tid, self._ready.qsize() + 1)
        return tid

    async def wait(self, tid: str, on_progress: Callable[[dict], None] | None = None) -> TaskRecord:
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
        recs = sorted(self._tasks.values(), key=lambda r: r.created_at, reverse=True)
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
            process_record = rec.meta.get("process_record")
            if process_record:
                from server import exec_process

                outcome = exec_process.terminate(
                    process_record,
                    workspace=rec.workspace or "",
                )
                if outcome.get("killed"):
                    logger.info(
                        "hicode 队列: 已终止 CLI process group → %s pids=%s",
                        tid,
                        outcome.get("pids"),
                    )
                    try:
                        await asyncio.wait_for(rec._done.wait(), timeout=12)
                    except TimeoutError:
                        logger.warning("hicode 队列: CLI 硬停后任务 %s 仍未收尾", tid)
                    return True
            # 1) 软中断: serve POST /cancel (秒级, 但模型调用可能不响应)
            from server.hicode_serve import get_serve_client

            client = get_serve_client()
            try:
                await client.cancel()
                logger.info("hicode 队列: 已请求 serve cancel → %s", tid)
            except Exception as exc:
                logger.warning("hicode 队列: serve cancel 失败 %s: %s", tid, exc)
            # 2) 软停观察窗口: 12s 内 turn 未中断 → 硬重启 serve (真正停止)
            try:
                await asyncio.wait_for(rec._done.wait(), timeout=12)
            except TimeoutError:
                logger.warning("hicode 队列: cancel 未中断 %s → 硬重启 serve", tid)
                try:
                    if not await client.restart_serve():
                        logger.warning("hicode 队列: serve 重启未恢复健康")
                except Exception as exc:
                    logger.warning("hicode 队列: serve 硬重启失败 %s: %s", tid, exc)
                # 等 worker 收尾 (events 断开 → run_task 返回)
                try:
                    await asyncio.wait_for(rec._done.wait(), timeout=30)
                except TimeoutError:
                    logger.warning("hicode 队列: 任务 %s 硬停后仍未收尾", tid)
            return True
        return False

    # ── worker ─────────────────────────────────────────────────────
    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop())

            def _on_done(t: asyncio.Task) -> None:
                if t.exception() and not isinstance(t.exception(), asyncio.CancelledError):
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
            except Exception as exc:
                logger.exception("hicode 任务 %s 异常", tid)
                rec.status = "failed"
                rec.error = str(exc)[:400]
            finally:
                process_record = rec.meta.pop("process_record", None)
                if process_record:
                    with contextlib.suppress(Exception):
                        await fs_delete(str(process_record))
                rec.updated_at = time.time()
                rec._done.set()

    async def _run_one(self, rec: TaskRecord) -> None:
        """Execute one queued item through GoalRun, preserving this ABI as a projection."""
        from server.goal_run.runner import project_run_goal

        class _HicodeGoalRunAdapter:
            verification_required = True
            skip_plan_review = True

            async def before_execution(self, state: Any, project_root: str) -> None:
                return None

            async def before_iteration(self, state: Any, project_root: str, task: Any) -> None:
                return None

            def checkpoint(self, state: Any, project_root: str, *, reason: str) -> None:
                save_goal_run(state, project_root)

            async def execute_semantic_task(self, state: Any, task: Any) -> LeafResult:
                from server.hicode_agent import _execute_hicode_core

                def _push(ev: dict) -> None:
                    rec.events.append(ev)
                    if len(rec.events) > 200:
                        rec.events.pop(0)
                    for watcher in list(rec._watchers):
                        with contextlib.suppress(Exception):
                            watcher(ev)

                from server import exec_process

                execution_root = (
                    Path(rec.workspace or os.environ.get("VEYA_PROJECT_ROOT", "."))
                    .expanduser()
                    .resolve()
                )
                process_record = (
                    execution_root / ".veya-project" / "hicode-processes" / f"{rec.id}.pid.json"
                )

                def _capture_process(pid: int, pgid: int) -> None:
                    exec_process.record(
                        process_record,
                        pid=pid,
                        pgid=pgid,
                        workspace=str(execution_root),
                    )
                    rec.meta["process_record"] = str(process_record)

                try:
                    from server.hicode_agent import bound_hicode_workspace

                    # The queue already received a session-authorized project
                    # root. Bind that root for the duration of the real CLI
                    # execution so hicode's resolver cannot fall back to its
                    # narrower process-global default workspace.
                    with bound_hicode_workspace(execution_root):
                        summary = await _execute_hicode_core(
                            rec.spec,
                            workspace=rec.workspace,
                            max_steps=int(rec.meta.get("max_steps") or 0),
                            timeout_sec=int(rec.meta.get("timeout_sec") or 900),
                            session_id=(
                                str(rec.meta["session_id"]) if rec.meta.get("session_id") else None
                            ),
                            continue_=bool(rec.meta.get("continue_")),
                            on_event=_push,
                            force_cli=bool(rec.meta.get("force_cli")),
                            on_process=_capture_process,
                        )
                except Exception as exc:
                    if getattr(exc, "failure_class", None) == "UPSTREAM_QUOTA_EXHAUSTED":
                        retry_at = getattr(exc, "retry_not_before", None)
                        rec.status = "blocked"
                        evidence = getattr(exc, "raw_evidence", {})
                        upstream_reset_at = evidence.get("upstream_quota_reset_at")
                        local_cooldown_until = evidence.get("local_cooldown_until")
                        rec.error = (
                            "MODEL_COOLDOWN\n"
                            f"UPSTREAM_QUOTA_RESET_AT={upstream_reset_at}\n"
                            f"LOCAL_PROXY_COOLDOWN_UNTIL={local_cooldown_until}\n"
                            f"EFFECTIVE_RETRY_NOT_BEFORE={retry_at}"
                            if retry_at is not None
                            else "MODEL_COOLDOWN"
                        )
                        rec.summary = rec.error
                        return LeafResult(
                            status="blocked",
                            summary=rec.summary,
                            block_reason=rec.error,
                            stop_reason="model_cooldown",
                        )
                    raise
                if rec.cancel_requested:
                    rec.status = "cancelled"
                    rec.error = "user stop"
                    rec.summary = summary[:400] if summary else ""
                    return LeafResult(status="blocked", summary=rec.summary, block_reason=rec.error)
                if summary.startswith("错误") or summary.startswith("hicode 不可用"):
                    rec.status = "failed"
                    rec.error = summary[:400]
                    rec.summary = summary
                    return LeafResult(status="blocked", summary=summary, block_reason=rec.error)
                rec.status = "done"
                rec.summary = summary
                return LeafResult(status="completed", summary=summary)

        try:
            goal_id = rec.meta.get("goal_id")
            response = await project_run_goal(
                project_root=rec.workspace or os.environ.get("VEYA_PROJECT_ROOT", "."),
                goal=f"Hicode task {rec.id}",
                tasks=[
                    {
                        "id": f"hicode:{rec.id}",
                        "title": f"Hicode task {rec.id}",
                        "instruction": _encode_goal_instruction(rec.spec, rec.meta),
                        "acceptance": ["Hicode provider returned a successful result"],
                        "assignee": "builtin",
                    }
                ],
                mode="act_eager",
                resume_goal_id=str(goal_id) if goal_id else None,
                integration_adapter=_HicodeGoalRunAdapter(),
            )
            rec.meta["goal_id"] = response.goal_id
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            rec.status = "failed"
            rec.error = str(exc)[:400]
        else:
            status = getattr(response.status, "value", response.status)
            if status not in {"completed", "partial_completed"}:
                if rec.status not in {"cancelled", "failed", "blocked"}:
                    rec.status = "failed"
                    rec.error = response.block_reason or "GoalRun did not complete"
            elif rec.status == "running":
                rec.status = "done"
                rec.summary = getattr(response, "summary", "") or rec.summary

    async def recover_goal_runs(self, project_root: str = ".") -> int:
        """Rebuild queue projections from unfinished Hicode GoalRuns."""
        root = Path(project_root) / ".veya-project" / "goal-runs"
        if not root.is_dir():
            return 0
        recovered = 0
        terminal = {"completed", "partial_completed", "failed", "cancelled", "blocked"}
        for graph in sorted(root.glob("*/taskgraph.json")):
            data = json.loads(graph.read_text(encoding="utf-8"))
            if data.get("status") in terminal:
                continue
            nodes = [
                item
                for item in data.get("tasks", [])
                if str(item.get("id", "")).startswith("hicode:")
            ]
            if not nodes:
                continue
            node = nodes[0]
            tid = str(node["id"])[len("hicode:") :]
            if tid in self._tasks:
                continue
            spec, persisted_meta = _decode_goal_instruction(str(node.get("instruction") or ""))
            persisted_meta.update({"goal_id": graph.parent.name, "recovered": True})
            rec = TaskRecord(
                id=tid,
                spec=spec,
                workspace=str(Path(project_root).expanduser().resolve()),
                meta=persisted_meta,
            )
            self._tasks[tid] = rec
            await self._ready.put(tid)
            recovered += 1
        if recovered:
            self._ensure_worker()
        return recovered


# 模块级单例 (server 复用)
hicode_task_queue = HicodeTaskQueue()
