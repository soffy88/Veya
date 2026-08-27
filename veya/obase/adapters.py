"""veya/obase/adapters — 阶段 1 薄适配器。

把现有实现适配为 :mod:`veya.obase.interfaces` 的严格句柄合同。
**适配器内不写业务逻辑**：只做形态转换（旧 API 形态 → 合同形态），
旧实现仍为单一事实源，阶段 3+ 逐个替换旧实现后适配器即退役。

- SandboxVfsAdapter    : veya.obase.sandbox.ProcessSandbox → VfsSandbox
- TelemetryEventBarrier: veya.obase.telemetry.emit         → EventBarrier
- SqliteKvStore        : 新实现（stdlib sqlite3，零依赖）   → KvStore
- LlmClientAdapter     : veya.obase.llm.llm_call/llm_stream → LlmClient
- InProcessDaemonBus   : 新实现（asyncio 进程内总线）       → DaemonBus
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from veya.obase import telemetry
from veya.obase.interfaces import Event, SandboxResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VfsSandbox 适配器
# ---------------------------------------------------------------------------


class SandboxVfsAdapter:
    """把 ProcessSandbox 适配为 VfsSandbox 合同。

    文件系统视图 = 沙盒工作目录（temp_dir，由 ProcessSandbox 创建）。
    所有路径操作强制限定在沙盒根内（PurePosixPath 前缀校验），越界拒绝。
    """

    def __init__(self, config: Any = None) -> None:
        from veya.obase.sandbox import SandboxConfig, create_sandbox

        self._config = config or SandboxConfig(network_blocked=True, allow_write=False)
        self._inner: Any = create_sandbox(self._config)
        self._closed = False

    # -- 内部工具 ----------------------------------------------------------

    def _root(self) -> Path:
        """沙盒文件系统根（懒初始化：首次文件操作/执行时创建）。"""
        if self._inner.temp_dir is None:
            self._inner.setup_environment()
        return Path(self._inner.temp_dir or tempfile.gettempdir())

    def _resolve(self, path: str) -> Path:
        """把合同路径解析到沙盒根内；越界抛 ValueError。"""
        root = self._root().resolve()
        target = (root / path).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"VFS 越界访问被拒绝: {path!r} (沙盒根: {root})")
        return target

    def _to_result(self, raw: dict[str, Any]) -> SandboxResult:
        code = int(raw.get("exit_code", -1))
        return SandboxResult(
            ok=code == 0,
            exit_code=code,
            stdout=str(raw.get("stdout", "")),
            stderr=str(raw.get("stderr", "")),
            command=str(raw.get("command", "")),
            duration_ms=float(raw.get("duration", 0.0)) * 1000.0,
            rejected=code == -3,
            audit=[e.to_dict() for e in self._inner.audit_log],
        )

    # -- 执行面 ------------------------------------------------------------

    async def execute(self, command: str, *, timeout: float | None = None) -> SandboxResult:
        raw = await self._inner.execute(command)
        return self._to_result(raw)

    async def execute_args(self, argv: list[str], *, timeout: float | None = None) -> SandboxResult:
        raw = await self._inner.execute_args(argv)
        return self._to_result(raw)

    async def run_script(self, script: str, *, timeout: float | None = None) -> SandboxResult:
        raw = await self._inner.run_script(script)
        return self._to_result(raw)

    async def cancel(self) -> None:
        await self._inner.cancel()

    # -- 文件面（VFS 权限范围内） -------------------------------------------

    async def read(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    async def write(self, path: str, data: bytes | str) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            target.write_text(data, encoding="utf-8")
        else:
            target.write_bytes(data)

    async def exists(self, path: str) -> bool:
        return self._resolve(path).exists()

    async def listdir(self, path: str) -> list[str]:
        return [p.name for p in self._resolve(path).iterdir()]

    async def delete(self, path: str) -> None:
        target = self._resolve(path)
        if target.is_dir():
            import shutil

            shutil.rmtree(target)
        else:
            target.unlink()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._inner.cleanup_environment()
            except Exception:
                logger.warning("sandbox cleanup failed", exc_info=True)


# ---------------------------------------------------------------------------
# EventBarrier 适配器
# ---------------------------------------------------------------------------


class TelemetryEventBarrier:
    """桥接 telemetry.emit + 进程内订阅扇出 + 名同步屏障。

    emit() 同时写入 telemetry（保持既有可观测性）并扇出到本地订阅队列。
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[Event]]] = {}
        self._barriers: dict[str, tuple[int, asyncio.Event]] = {}
        self._lock = asyncio.Lock()

    def emit(self, event: Event) -> None:
        # 既有遥测通道保持打通（JSONL / set_emitter 回调）
        with contextlib.suppress(Exception):
            telemetry.emit({"topic": event.topic, "payload": event.payload})
        for q in self._subscribers.get(event.topic, []):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    def stream(self, *topics: str) -> AsyncIterator[Event]:
        """按 topic 订阅（多 topic 取并集）；迭代器关闭自动退订。"""

        async def _gen() -> AsyncIterator[Event]:
            q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1024)
            registered = set(topics)
            for t in topics:
                self._subscribers.setdefault(t, []).append(q)
            try:
                while True:
                    event = await q.get()
                    if event.topic not in registered:
                        continue  # 别的订阅者队列误投，跳过
                    yield event
            finally:
                for t in topics:
                    qs = self._subscribers.get(t)
                    if qs and q in qs:
                        qs.remove(q)

        return _gen()

    async def barrier(self, name: str, parties: int, *, timeout: float | None = None) -> None:
        if parties < 1:
            raise ValueError("parties 必须 >= 1")
        async with self._lock:
            entry = self._barriers.get(name)
            if entry is None:
                entry = (0, asyncio.Event())
                self._barriers[name] = entry
            count, gate = entry
            if count >= parties:
                raise ValueError(f"barrier {name!r} parties 不一致（已有 {count} 个到达）")
            count += 1
            if count == parties:
                gate.set()
            self._barriers[name] = (count, gate)
        try:
            await asyncio.wait_for(gate.wait(), timeout=timeout)
        except TimeoutError:
            async with self._lock:
                self._barriers.pop(name, None)
            raise


# ---------------------------------------------------------------------------
# KvStore 实现（SQLite，stdlib）
# ---------------------------------------------------------------------------


class SqliteKvStore:
    """Session Tree 快照 KV（JSON 值）。默认 :memory:；生产接线可给文件路径。"""

    def __init__(self, path: str = ":memory:") -> None:
        # check_same_thread=False + 显式锁: 实例常作为长期单例被注入, 调用方可能
        # 经 run_sync_in_daemon_thread 从工作线程访问同一连接 (与
        # SqliteHistoryStore._connect 同款理由)。
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._conn.commit()

    @staticmethod
    def _encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, self._encode(value)),
            )
            self._conn.commit()

    def get(self, key: str) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM kv WHERE key=?", (key,))
            self._conn.commit()

    def keys(self, prefix: str = "") -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key FROM kv WHERE key LIKE ? ORDER BY key", (prefix + "%",)
            ).fetchall()
        return [r[0] for r in rows]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM kv").fetchall()
        return {k: json.loads(v) for k, v in rows}

    def restore(self, data: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM kv")
            self._conn.executemany(
                "INSERT INTO kv(key, value) VALUES(?, ?)",
                [(k, self._encode(v)) for k, v in data.items()],
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# LlmClient 适配器
# ---------------------------------------------------------------------------


class LlmClientAdapter:
    """把 veya.obase.llm.llm_call / llm_stream 适配为 LlmClient 合同。

    合同要求：只发已打包数据（messages + 传输参数），无 Prompt 逻辑 ——
    现有 llm_call 内部仍含 provider/model 解析与 veya1.1 别名路由
    （属通道配置而非 Prompt 业务，阶段 3 下沉 oprim_llm_call 时收敛）。
    """

    def __init__(self) -> None:
        from veya.obase.llm import llm_call, llm_stream

        self._complete = llm_call
        self._stream = llm_stream

    async def complete(self, messages: list[dict], **kwargs: Any) -> dict:
        return await self._complete(messages, **kwargs)

    def stream(self, messages: list[dict], **kwargs: Any) -> AsyncIterator[dict]:
        return self._stream(messages, **kwargs)

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# DaemonBus 实现（进程内）
# ---------------------------------------------------------------------------


class InProcessDaemonBus:
    """进程内长连接总线：asyncio Pub/Sub + 请求-响应。

    合同与未来 gRPC/WebSocket 总线一致，替换实现无需改业务代码。
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[Event]]] = {}
        self._handlers: dict[str, Any] = {}
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._closed = False

    async def connect(self) -> None:
        self._closed = False

    async def close(self) -> None:
        self._closed = True
        self._subscribers.clear()
        for fut in self._pending.values():
            fut.cancel()
        self._pending.clear()

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("bus 已关闭")
        event = Event(topic=topic, payload=payload)
        for q in list(self._subscribers.get(topic, [])):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    def subscribe(self, topic: str) -> AsyncIterator[Event]:
        async def _gen() -> AsyncIterator[Event]:
            q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1024)
            self._subscribers.setdefault(topic, []).append(q)
            try:
                while True:
                    yield await q.get()
            finally:
                qs = self._subscribers.get(topic)
                if qs and q in qs:
                    qs.remove(q)

        return _gen()

    async def request(
        self, topic: str, payload: dict[str, Any], *, timeout: float = 30.0
    ) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[request_id] = fut
        handler = self._handlers.get(topic)
        try:
            if handler is None:
                raise TimeoutError(f"topic {topic!r} 无处理器")
            reply = await asyncio.wait_for(handler(payload, request_id=request_id), timeout=timeout)
            return cast("dict[str, Any]", reply)
        finally:
            self._pending.pop(request_id, None)

    async def register_handler(self, topic: str, handler: Any) -> None:
        self._handlers[topic] = handler


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

__all__ = [
    "InProcessDaemonBus",
    "LlmClientAdapter",
    "SandboxVfsAdapter",
    "SqliteKvStore",
    "TelemetryEventBarrier",
]
