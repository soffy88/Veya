"""阶段 1 回归: 严格句柄层 — 现有能力经适配器跑通 (行为不变)。

覆盖 5 个合同 + 全局单例句柄层:
- VfsSandbox   : ProcessSandbox 经适配器执行/文件面/VFS 越界拦截
- EventBarrier : telemetry.emit 桥接 + 订阅扇出 + 同步屏障
- KvStore      : SQLite 快照 put/get/snapshot/restore
- LlmClient    : llm_call/llm_stream 适配 (stub 回落, 无网络)
- DaemonBus    : 进程内 Pub/Sub + 请求-响应
- container    : 单例句柄 + configure 注入 + reset
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from veya.obase import container
from veya.obase.adapters import (
    InProcessDaemonBus,
    SandboxVfsAdapter,
    SqliteKvStore,
    TelemetryEventBarrier,
)
from veya.obase.interfaces import Event, SandboxResult


# ---------------------------------------------------------------------------
# VfsSandbox 适配器 (现有 ProcessSandbox)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vfs_sandbox_execute_echo():
    sb = SandboxVfsAdapter()
    try:
        res: SandboxResult = await sb.execute("echo hello-sandbox")
        assert res.ok
        assert res.exit_code == 0
        assert "hello-sandbox" in res.stdout
    finally:
        await sb.close()


@pytest.mark.asyncio
async def test_vfs_sandbox_dangerous_command_rejected():
    sb = SandboxVfsAdapter()
    try:
        res = await sb.execute("rm -rf /")
        assert not res.ok
        assert res.rejected
        assert res.exit_code == -3
    finally:
        await sb.close()


@pytest.mark.asyncio
async def test_vfs_sandbox_execute_args_no_shell():
    sb = SandboxVfsAdapter()
    try:
        res = await sb.execute_args(["echo", "safe-argv"])
        assert res.ok
        assert "safe-argv" in res.stdout
    finally:
        await sb.close()


@pytest.mark.asyncio
async def test_vfs_sandbox_filesystem_roundtrip_and_escape_block():
    sb = SandboxVfsAdapter()
    try:
        await sb.write("dir/nested/note.txt", "vfs-content")
        assert await sb.exists("dir/nested/note.txt")
        assert (await sb.read("dir/nested/note.txt")) == b"vfs-content"
        assert "note.txt" in await sb.listdir("dir/nested")
        await sb.delete("dir/nested/note.txt")
        assert not await sb.exists("dir/nested/note.txt")
        # VFS 越界必须拒绝
        with pytest.raises(ValueError):
            await sb.write("../escape.txt", "x")
        with pytest.raises(ValueError):
            await sb.read("../../etc/passwd")
    finally:
        await sb.close()


# ---------------------------------------------------------------------------
# EventBarrier (telemetry 桥接)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_barrier_pubsub():
    b = TelemetryEventBarrier()
    ev = Event(topic="tool_call", payload={"tool_name": "write"})
    async with asyncio.timeout(2):
        task = asyncio.create_task(anext(b.stream("tool_call")))
        await asyncio.sleep(0.05)
        b.emit(ev)
        got = await task
    assert got.topic == "tool_call"
    assert got.payload["tool_name"] == "write"


@pytest.mark.asyncio
async def test_event_barrier_rendezvous_and_timeout():
    b = TelemetryEventBarrier()

    async def arrive(name: str, parties: int) -> None:
        await b.barrier(name, parties)

    results: list[bool] = []

    async def runner(name: str, parties: int) -> None:
        try:
            await arrive(name, parties)
            results.append(True)
        except asyncio.TimeoutError:
            results.append(False)

    t1 = asyncio.create_task(runner("sync", 2))
    t2 = asyncio.create_task(runner("sync", 2))
    await asyncio.gather(t1, t2)
    assert results == [True, True]

    # 只有一方到达 → 超时
    t3 = asyncio.create_task(runner("lonely", 2))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(t3, timeout=1.0)


# ---------------------------------------------------------------------------
# KvStore (SQLite)
# ---------------------------------------------------------------------------


def test_kv_store_roundtrip_and_snapshot():
    kv = SqliteKvStore()
    try:
        kv.put("session:root", {"id": "root", "parent": None, "leaf": True})
        kv.put("session:branch:a", {"id": "a", "parent": "root"})
        assert kv.get("session:root")["leaf"] is True
        assert set(kv.keys("session:")) == {"session:root", "session:branch:a"}
        snap = kv.snapshot()
        assert len(snap) == 2
        kv.put("session:extra", [1, 2, 3])
        kv.restore(snap)
        assert kv.get("session:extra") is None  # restore 为原子整树替换
        assert kv.get("session:root")["id"] == "root"
        kv.delete("session:root")
        assert kv.get("session:root") is None
    finally:
        kv.close()


# ---------------------------------------------------------------------------
# LlmClient 适配器 (stub 回落, 无网络)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_client_complete_stub_fallback(monkeypatch: pytest.MonkeyPatch):
    from veya.obase import llm as obase_llm
    from veya.obase.adapters import LlmClientAdapter

    # 隔离宿主用户配置 + 清空所有 key → 走 stub 回落 (确定性, 无网络)
    monkeypatch.setattr(obase_llm, "_user_llm_config", lambda: {})
    for var in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VEYA_LLM_PROVIDER", "openai")

    client = LlmClientAdapter()
    try:
        resp = await client.complete([{"role": "user", "content": "hi"}])
        assert "choices" in resp
        assert resp["choices"][0]["message"]["content"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_llm_client_stream_yields_deltas(monkeypatch: pytest.MonkeyPatch):
    from veya.obase import llm as obase_llm
    from veya.obase.adapters import LlmClientAdapter

    monkeypatch.setattr(obase_llm, "_user_llm_config", lambda: {})
    for var in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VEYA_LLM_PROVIDER", "openai")

    client = LlmClientAdapter()
    try:
        chunks = [c async for c in client.stream([{"role": "user", "content": "hi"}])]
        assert len(chunks) >= 1
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# DaemonBus (进程内)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_bus_pubsub():
    bus = InProcessDaemonBus()
    await bus.connect()
    try:

        async def collect() -> list[Event]:
            out = []
            async for ev in bus.subscribe("hicode_progress"):
                out.append(ev)
                if len(out) == 2:
                    break
            return out

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        await bus.publish("hicode_progress", {"step": 1})
        await bus.publish("hicode_progress", {"step": 2})
        got = await asyncio.wait_for(task, timeout=2)
        assert [e.payload["step"] for e in got] == [1, 2]
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_daemon_bus_request_reply():
    bus = InProcessDaemonBus()

    async def pause_handler(payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return {"status": "paused", "session": payload.get("session_id")}

    await bus.register_handler("daemon.pause", pause_handler)
    try:
        reply = await bus.request("daemon.pause", {"session_id": "s1"}, timeout=1)
        assert reply == {"status": "paused", "session": "s1"}
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_daemon_bus_request_timeout_without_handler():
    bus = InProcessDaemonBus()
    try:
        with pytest.raises(TimeoutError):
            await bus.request("no.handler", {}, timeout=0.2)
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# container — 全局单例句柄层
# ---------------------------------------------------------------------------


def test_container_default_singletons():
    container.reset()
    try:
        assert container.get_sandbox() is container.get_sandbox()
        assert container.get_bus() is container.get_bus()
        assert container.get_barrier() is container.get_barrier()
        assert container.get_kv() is container.get_kv()
        assert container.get_llm() is container.get_llm()
    finally:
        container.close_all()


def test_container_configure_override_and_reset():
    container.reset()
    try:
        stub = object()
        container.configure(sandbox=stub)
        assert container.get_sandbox() is stub
        container.reset()
        assert container.get_sandbox() is not stub
    finally:
        container.close_all()


def test_container_configure_rejects_unknown():
    container.reset()
    with pytest.raises(ValueError):
        container.configure(not_a_handle=object())
