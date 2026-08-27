"""阶段 5 回归: oservi 长时守护引擎 + 统一网关 + AgentLoop gate 检查点。

覆盖:
- DaemonEngine: 后台任务 (独立 AgentLoop) / 状态机 / pause-resume (HITL) /
  人类输入注入 / DaemonBus 集成 (oprim.daemon 原子) / 事件流中继
- AgentLoop gate: 挂起检查点 (paused 阻塞等待 resume)
- Gateway: FastAPI 极简指令 (POST/GET/pause/resume/SSE) + 404/409
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from veya.obase.adapters import InProcessDaemonBus, SqliteKvStore, TelemetryEventBarrier
from veya.omodul.agent_loop import AgentLoop
from veya.omodul.session_tree import SessionTreeMgr
from veya.omodul.tool_pipeline import ToolPipeline
from veya.oprim.daemon import daemon_pause, daemon_resume, daemon_status
from veya.oservi.daemon_engine import DaemonEngine, TaskStatus


class FakeLlm:
    def __init__(self, script: list[dict], delay: float = 0.0) -> None:
        self._script = script
        self._calls = 0
        self._delay = delay

    async def complete(self, messages: list[dict], **kwargs: Any) -> dict:
        if self._delay:
            await asyncio.sleep(self._delay)
        reply = self._script[min(self._calls, len(self._script) - 1)]
        self._calls += 1
        return {"choices": [{"message": reply}]}

    async def close(self) -> None:
        pass


def _tool_msg(name: str, args: dict) -> dict:
    return {
        "role": "assistant",
        "content": "using tool",
        "tool_calls": [
            {
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        ],
    }


def _engine(
    *, bus: Any = None, barrier: Any = None, llm: Any = None, tree: Any = None
) -> DaemonEngine:
    return DaemonEngine(
        bus=bus,
        barrier=barrier,
        llm=llm,
        tree=tree or SessionTreeMgr(kv=SqliteKvStore()),
    )


async def _wait_status(
    engine: DaemonEngine, task_id: str, *statuses: TaskStatus, timeout: float = 5.0
) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            st = await engine.status(task_id)
            if st["status"] in {s.value for s in statuses}:
                return st
            await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# DaemonEngine — 后台任务
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_runs_task_to_completion():
    llm = FakeLlm(
        [
            _tool_call(_tool_msg("add", {"a": 2, "b": 3})),
            {"role": "assistant", "content": "答案是 5"},
        ]
    )
    engine = _engine(llm=llm)
    engine.register_tool(
        "add",
        lambda a, b: a + b,
        schema={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        },
    )
    state = await engine.create_task("2+3?")
    st = await _wait_status(engine, state.task_id, TaskStatus.COMPLETED)
    assert st["status"] == "completed"
    assert st["final_answer"] == "答案是 5"
    assert st["rounds"] == 2
    await engine.shutdown()


def _tool_call(msg: dict) -> dict:
    return msg


@pytest.mark.asyncio
async def test_engine_pause_resume_hitl():
    """gate 检查点: paused 后任务阻塞, resume 后继续完成。"""
    llm = FakeLlm(
        [
            _tool_call(_tool_msg("echo", {"text": "x"})),
            {"role": "assistant", "content": "任务完成啦"},
        ]
    )
    engine = _engine(llm=llm)
    engine.register_tool(
        "echo",
        lambda text: text,
        schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )
    state = await engine.create_task("任务")
    # 立即挂起（首轮 gate 前）
    r = await engine.pause(state.task_id)
    assert r["status"] == "paused"
    await asyncio.sleep(0.05)
    assert (await engine.status(state.task_id))["status"] == "paused"
    # 恢复 → 完成
    await engine.resume(state.task_id)
    st = await _wait_status(engine, state.task_id, TaskStatus.COMPLETED)
    assert st["final_answer"] == "任务完成啦"
    await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_submit_human_input():
    """HITL: paused 时注入人类输入 → 写入会话树 → 恢复后模型看到新输入。"""
    seen: list[list] = []

    class CaptureLlm:
        async def complete(self, messages, **kw):
            seen.append(list(messages))
            return {"choices": [{"message": {"role": "assistant", "content": "收到"}}]}

    tree = SessionTreeMgr(kv=SqliteKvStore())
    engine = _engine(llm=CaptureLlm(), tree=tree)
    state = await engine.create_task("开始")
    await engine.pause(state.task_id)
    await asyncio.sleep(0.03)
    await engine.resume(state.task_id, input_text="人类补充: 注意安全")
    await _wait_status(engine, state.task_id, TaskStatus.COMPLETED)
    # 人类输入作为 user 节点入树
    contents = [n["content"] for n in tree.path(state.session_id)]
    assert "人类补充: 注意安全" in contents
    await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_bus_integration():
    """oprim.daemon 原子经 DaemonBus 直达引擎（阶段 3 原子接通真实链路）。"""
    bus = InProcessDaemonBus()
    llm = FakeLlm(
        [
            _tool_call(_tool_msg("ping", {})),
            {"role": "assistant", "content": "pong"},
        ]
    )
    engine = _engine(bus=bus, llm=llm)
    engine.register_tool("ping", lambda: "pong", schema={"type": "object"})
    await engine.start()
    try:
        state = await engine.create_task("任务")
        # bus → pause
        r = await daemon_pause(bus=bus, session_id=state.task_id)
        assert r["status"] == "paused"
        assert (await engine.status(state.task_id))["status"] == "paused"
        # bus → resume
        await daemon_resume(bus=bus, session_id=state.task_id)
        # bus → status
        st = await daemon_status(bus=bus, session_id=state.task_id)
        assert st["status"] in ("running", "completed")
        await _wait_status(engine, state.task_id, TaskStatus.COMPLETED)
        assert (await daemon_status(bus=bus, session_id=state.task_id))["status"] == "completed"
    finally:
        await engine.shutdown()
        await bus.close()


@pytest.mark.asyncio
async def test_engine_stream_events():
    """事件中继: stream(task_id) 收到 agent_loop 事件（慢 LLM 留订阅窗口）。"""
    barrier = TelemetryEventBarrier()
    llm = FakeLlm([{"role": "assistant", "content": "直接回答"}], delay=0.1)
    engine = _engine(barrier=barrier, llm=llm)
    await engine.start()
    try:
        state = await engine.create_task("hi")
        events = []
        async for ev in engine.stream(state.task_id):
            events.append(ev)
            if ev.get("type") == "agent_loop.done":
                break
        types = [e["type"] for e in events]
        assert "agent_loop.done" in types
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_errors_and_status():
    engine = _engine(llm=FakeLlm([{"role": "assistant", "content": "ok"}]))
    with pytest.raises(KeyError):
        await engine.status("no-such")
    state = await engine.create_task("x")
    with pytest.raises(RuntimeError):  # 已完成不能再 pause
        await engine.pause(state.task_id) if (
            await _wait_status(engine, state.task_id, TaskStatus.COMPLETED)
        ) else None
    await engine.shutdown()


# ---------------------------------------------------------------------------
# AgentLoop gate — 挂起检查点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_loop_gate_blocks_and_releases():
    """gate: 首轮前阻塞, 事件放行后完成。"""
    gate_ev = asyncio.Event()
    released = []

    async def gate():
        released.append("hit")
        await gate_ev.wait()

    llm = FakeLlm([{"role": "assistant", "content": "检查点放行完成"}])
    loop = AgentLoop(
        llm=llm, pipeline=ToolPipeline(), tree=SessionTreeMgr(kv=SqliteKvStore()), gate=gate
    )
    task = asyncio.create_task(loop.run("hi"))
    await asyncio.sleep(0.05)
    assert released == ["hit"]  # 已停在检查点
    assert not task.done()
    gate_ev.set()
    result = await asyncio.wait_for(task, timeout=2)
    assert result.stop_kind == "completed"
    assert result.final_answer == "检查点放行完成"


# ---------------------------------------------------------------------------
# Gateway — FastAPI 极简指令 (httpx ASGITransport, 不触发 app lifespan)
# ---------------------------------------------------------------------------


@pytest.fixture
async def gateway_client():
    import httpx

    from server.app import app
    from veya.oservi.gateway import gateway_engine

    engine = _engine(
        llm=FakeLlm(
            [
                {"role": "assistant", "content": "网关回答啦"},
            ],
            delay=0.05,
        )
    )
    gateway_engine(engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.shutdown()


@pytest.mark.asyncio
async def test_gateway_create_and_query(gateway_client):
    r = await gateway_client.post("/api/v1/3o/tasks", json={"user_input": "你好"})
    assert r.status_code == 201
    task_id = r.json()["task_id"]
    for _ in range(100):
        st = (await gateway_client.get(f"/api/v1/3o/tasks/{task_id}")).json()
        if st["status"] == "completed":
            break
        await asyncio.sleep(0.02)
    assert st["status"] == "completed"
    assert st["final_answer"] == "网关回答啦"


@pytest.mark.asyncio
async def test_gateway_pause_resume_flow(gateway_client):
    from veya.oservi.gateway import gateway_engine

    gateway_engine()
    r = await gateway_client.post("/api/v1/3o/tasks", json={"user_input": "任务"})
    task_id = r.json()["task_id"]
    assert (await gateway_client.post(f"/api/v1/3o/tasks/{task_id}/pause")).status_code == 200
    assert (await gateway_client.get(f"/api/v1/3o/tasks/{task_id}")).json()["status"] == "paused"
    # 恢复 + 人类输入注入
    r2 = await gateway_client.post(f"/api/v1/3o/tasks/{task_id}/resume", json={"input": "继续吧"})
    assert r2.status_code == 200
    for _ in range(100):
        st = (await gateway_client.get(f"/api/v1/3o/tasks/{task_id}")).json()
        if st["status"] == "completed":
            break
        await asyncio.sleep(0.02)
    assert st["status"] == "completed"


@pytest.mark.asyncio
async def test_gateway_errors(gateway_client):
    assert (await gateway_client.get("/api/v1/3o/tasks/nope")).status_code == 404
    r = await gateway_client.post("/api/v1/3o/tasks", json={"user_input": ""})
    assert r.status_code == 422
    assert (await gateway_client.get("/api/v1/3o/tasks/nope/stream")).status_code == 404
