"""阶段 3 回归: oprim 物理触手原子操作 (6 组, 注入句柄)。

覆盖:
- fs 原子 (经 VfsSandbox: 读/写/存在/列表/删除 + 越界拒绝)
- shell 原子 (执行/argv/脚本 + 危险拦截)
- snapshot 原子 (经 KvStore: commit/fetch/list/delete)
- event 原子 (经 EventBarrier: emit → stream 收到)
- llm 原子 (经 LlmClient: complete/stream, stub 回落)
- daemon 原子 (经 DaemonBus: pause/resume/status RPC)
- 注入优先: 显式句柄 > container 全局句柄
"""

from __future__ import annotations

import pytest

from veya.obase.adapters import (
    InProcessDaemonBus,
    SandboxVfsAdapter,
    SqliteKvStore,
    TelemetryEventBarrier,
)
from veya.obase.interfaces import SandboxResult
from veya.oprim import (
    daemon_pause,
    daemon_resume,
    daemon_status,
    emit_event,
    fs_delete,
    fs_exists,
    fs_listdir,
    fs_read,
    fs_read_text,
    fs_write,
    fs_write_text,
    llm_call,
    llm_stream,
    shell_exec,
    shell_exec_args,
    shell_run_script,
    snapshot_commit,
    snapshot_delete,
    snapshot_fetch,
    snapshot_list,
)

# ---------------------------------------------------------------------------
# fs 原子
# ---------------------------------------------------------------------------


@pytest.fixture
async def sandbox():
    sb = SandboxVfsAdapter()
    yield sb
    await sb.close()


@pytest.mark.asyncio
async def test_fs_write_read_roundtrip(sandbox):
    await fs_write("notes/a.txt", "hello-atom", sandbox=sandbox)
    assert await fs_exists("notes/a.txt", sandbox=sandbox)
    assert await fs_read("notes/a.txt", sandbox=sandbox) == b"hello-atom"
    assert await fs_read_text("notes/a.txt", sandbox=sandbox) == "hello-atom"
    assert "a.txt" in await fs_listdir("notes", sandbox=sandbox)


@pytest.mark.asyncio
async def test_fs_write_text_and_delete(sandbox):
    await fs_write_text("x.txt", "text-content", sandbox=sandbox)
    assert await fs_read_text("x.txt", sandbox=sandbox) == "text-content"
    await fs_delete("x.txt", sandbox=sandbox)
    assert not await fs_exists("x.txt", sandbox=sandbox)


@pytest.mark.asyncio
async def test_fs_escape_rejected(sandbox):
    with pytest.raises(ValueError):
        await fs_write("../escape.txt", "x", sandbox=sandbox)
    with pytest.raises(ValueError):
        await fs_read("../../etc/passwd", sandbox=sandbox)


@pytest.mark.asyncio
async def test_fs_uses_container_default():
    from veya.obase import container

    container.reset()
    try:
        await fs_write("c.txt", "via-container")
        assert await fs_read_text("c.txt") == "via-container"
    finally:
        await container.aclose_all()


# ---------------------------------------------------------------------------
# shell 原子
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shell_exec_echo(sandbox):
    res: SandboxResult = await shell_exec("echo atom-shell", sandbox=sandbox)
    assert res.ok
    assert "atom-shell" in res.stdout


@pytest.mark.asyncio
async def test_shell_exec_dangerous_rejected(sandbox):
    res = await shell_exec("rm -rf /", sandbox=sandbox)
    assert not res.ok
    assert res.rejected


@pytest.mark.asyncio
async def test_shell_exec_args_no_shell(sandbox):
    res = await shell_exec_args(["echo", "safe"], sandbox=sandbox)
    assert res.ok
    assert "safe" in res.stdout


@pytest.mark.asyncio
async def test_shell_run_script(sandbox):
    # run_script = 沙盒内写脚本 + python 执行 (argv 形态, 无 shell 拼接)
    res = await shell_run_script("print('script-ran')", sandbox=sandbox)
    assert res.ok
    assert "script-ran" in res.stdout


# ---------------------------------------------------------------------------
# snapshot 原子
# ---------------------------------------------------------------------------


def test_snapshot_commit_fetch_list_delete():
    kv = SqliteKvStore()
    try:
        snapshot_commit("root", {"id": "root", "leaf": True}, kv=kv)
        snapshot_commit("branch-a", {"id": "branch-a", "parent": "root"}, kv=kv)
        assert snapshot_fetch("root", kv=kv) == {"id": "root", "leaf": True}
        assert set(snapshot_list(kv=kv)) == {"root", "branch-a"}
        snapshot_delete("branch-a", kv=kv)
        assert snapshot_fetch("branch-a", kv=kv) is None
        assert snapshot_list(kv=kv) == ["root"]
    finally:
        kv.close()


def test_snapshot_uses_container_default():
    from veya.obase import container

    container.reset()
    try:
        snapshot_commit("s1", {"v": 1})
        assert snapshot_fetch("s1") == {"v": 1}
    finally:
        container.close_all()


# ---------------------------------------------------------------------------
# event 原子
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_event_reaches_stream():
    import asyncio

    barrier = TelemetryEventBarrier()
    # 先订阅 (生成器首轮迭代时注册队列), 再 emit
    task = asyncio.create_task(anext(barrier.stream("tool_progress")))
    await asyncio.sleep(0.05)
    ev = emit_event("tool_progress", {"step": 3}, barrier=barrier)
    assert ev.topic == "tool_progress"
    async with asyncio.timeout(2):
        got = await task
    assert got.payload["step"] == 3


def test_emit_event_uses_container_default():
    from veya.obase import container

    container.reset()
    try:
        ev = emit_event("default_topic", {"ok": True})
        assert ev.topic == "default_topic"
    finally:
        container.close_all()


# ---------------------------------------------------------------------------
# llm 原子
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_call_atom_stub(monkeypatch: pytest.MonkeyPatch):
    from veya.obase import llm as obase_llm

    monkeypatch.setattr(obase_llm, "_user_llm_config", lambda: {})
    for var in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VEYA_LLM_PROVIDER", "openai")

    resp = await llm_call([{"role": "user", "content": "hi"}])
    assert "choices" in resp
    assert resp["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_llm_stream_atom(monkeypatch: pytest.MonkeyPatch):
    from veya.obase import llm as obase_llm

    monkeypatch.setattr(obase_llm, "_user_llm_config", lambda: {})
    for var in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VEYA_LLM_PROVIDER", "openai")

    chunks = [c async for c in llm_stream([{"role": "user", "content": "hi"}])]
    assert len(chunks) >= 1


# ---------------------------------------------------------------------------
# daemon 原子
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_pause_resume_status():
    bus = InProcessDaemonBus()

    async def on_pause(payload, **kw):
        return {
            "status": "paused",
            "session": payload["session_id"],
            "reason": payload.get("reason"),
        }

    async def on_resume(payload, **kw):
        return {"status": "running", "session": payload["session_id"]}

    async def on_status(payload, **kw):
        return {"status": "running", "session": payload["session_id"]}

    await bus.register_handler("daemon.pause", on_pause)
    await bus.register_handler("daemon.resume", on_resume)
    await bus.register_handler("daemon.status", on_status)
    try:
        r = await daemon_pause(bus=bus, session_id="s9", reason="await human")
        assert r == {"status": "paused", "session": "s9", "reason": "await human"}
        r = await daemon_resume(bus=bus, session_id="s9")
        assert r["status"] == "running"
        r = await daemon_status(bus=bus, session_id="s9")
        assert r["status"] == "running"
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_daemon_atom_timeout_without_handler():
    bus = InProcessDaemonBus()
    try:
        with pytest.raises(TimeoutError):
            await daemon_status(bus=bus, session_id="x", timeout=0.2)
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class asyncio_timeout:
    """minimal asyncio timeout context (3.11+ compatible)."""

    def __init__(self, seconds: float):

        self._seconds = seconds

    async def __aenter__(self):
        import asyncio

        self._task = asyncio.current_task()
        self._timer = asyncio.create_task(asyncio.sleep(self._seconds))
        return self

    async def __aexit__(self, *exc):
        self._timer.cancel()
        return False
