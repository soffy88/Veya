"""veya/oservi/daemon_engine — oservi_daemon_engine（长时任务守护引擎）。

常驻后台，为每个任务实例化独立的 omodul_agent_loop：

    create_task(user_input)  → 后台 asyncio task 驱动 AgentLoop（注入 gate 检查点）
    pause / resume / status  → 状态机 PENDING→RUNNING⇄PAUSED→COMPLETED/FAILED
    submit_input(task, text) → 人类输入注入（paused 时写入会话树后恢复）
    stream(task)             → 任务事件流（agent_loop.* 事件按 task 中继）

DaemonBus 集成（阶段 3 oprim_daemon 原子接通真实链路）:
    start() 后自动注册 daemon.pause / daemon.resume / daemon.status 处理器，
    任何经 bus 的调用（oprim.daemon.daemon_pause(bus=...)）即可挂起/恢复/查询。

注入: bus / barrier / llm / pipeline / tree / system_prompt / max_rounds
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from veya.omodul.agent_loop import AgentLoop, LoopResult
from veya.omodul.session_tree import SessionTreeMgr
from veya.omodul.tool_pipeline import ToolPipeline
from veya.oprim.daemon import daemon_bind
from veya.oprim.event import emit_event

# 中继关注的 agent_loop 事件 topic（事件 payload 需带 session_id）
_RELAY_TOPICS: tuple[str, ...] = (
    "agent_loop.start",
    "agent_loop.round",
    "agent_loop.tool_result",
    "agent_loop.done",
    "task.end",  # 任务终止信号（经事件流保证顺序在 agent_loop.done 之后）
)


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskState:
    """单个任务的守护状态。"""

    task_id: str
    user_input: str
    status: TaskStatus = TaskStatus.PENDING
    session_id: str = ""
    error: str = ""
    result: LoopResult | None = None
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)
    _resume_event: asyncio.Event = field(default_factory=asyncio.Event)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "session_id": self.session_id,
            "error": self.error,
            "rounds": self.result.rounds if self.result else 0,
            "final_answer": self.result.final_answer if self.result else "",
            "stop_kind": self.result.stop_kind if self.result else "",
        }


class DaemonEngine:
    """长时任务守护引擎：任务状态机 + 独立 AgentLoop + HITL 挂起/恢复。"""

    def __init__(
        self,
        *,
        bus: Any = None,
        barrier: Any = None,
        llm: Any = None,
        pipeline_factory: Callable[[], ToolPipeline] | None = None,
        tree: SessionTreeMgr | None = None,
        system_prompt: str = "",
        max_rounds: int = 10,
    ) -> None:
        self._bus = bus
        self._barrier = barrier
        self._llm = llm
        self._pipeline_factory = pipeline_factory or ToolPipeline
        self._tree = tree or SessionTreeMgr()
        self._system_prompt = system_prompt
        self._max_rounds = max_rounds
        self._tasks: dict[str, TaskState] = {}
        self._drivers: dict[str, asyncio.Task] = {}
        self._relay_task: asyncio.Task | None = None
        self._fans: dict[str, list[asyncio.Queue]] = {}
        self._tool_specs: dict[str, tuple[Callable[..., Any], dict | None]] = {}

    def register_tool(self, name: str, fn: Callable[..., Any], schema: dict | None = None) -> None:
        """注册共享工具（所有任务实例的 pipeline 继承）。"""
        self._tool_specs[name] = (fn, schema)

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """注册 DaemonBus 处理器（daemon.pause/resume/status）+ 启动事件中继。"""
        await daemon_bind(bus=self._bus, topic="daemon.pause", handler=self._on_bus_pause)
        await daemon_bind(bus=self._bus, topic="daemon.resume", handler=self._on_bus_resume)
        await daemon_bind(bus=self._bus, topic="daemon.status", handler=self._on_bus_status)
        if self._barrier is not None and self._relay_task is None:
            self._relay_task = asyncio.create_task(self._relay_loop())

    async def shutdown(self) -> None:
        """取消全部任务驱动器与事件中继。"""
        for task in self._drivers.values():
            task.cancel()
        self._drivers.clear()
        if self._relay_task is not None:
            self._relay_task.cancel()
            self._relay_task = None
        self._tasks.clear()

    # ------------------------------------------------------------------ 任务操作

    async def create_task(
        self,
        user_input: str,
        *,
        tools: dict[str, tuple[Callable[..., Any], dict | None]] | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> TaskState:
        """提交任务：后台独立 AgentLoop 实例执行。"""
        task_id = task_id or uuid.uuid4().hex[:12]
        state = TaskState(task_id=task_id, user_input=user_input)
        self._tasks[task_id] = state
        # 预建会话（事件中继按 session_id 分发 + HITL 输入注入需要）
        state.session_id = session_id or self._tree.create_session(
            system=self._system_prompt or None
        )

        pipeline = self._pipeline_factory()
        for name, (fn, schema) in self._tool_specs.items():
            pipeline.register(name, fn, schema=schema)
        for name, (fn, schema) in (tools or {}).items():
            pipeline.register(name, fn, schema=schema)

        loop = AgentLoop(
            llm=self._llm,
            pipeline=pipeline,
            tree=self._tree,
            barrier=self._barrier,
            system_prompt=self._system_prompt,
            max_rounds=self._max_rounds,
            gate=self._make_gate(state),
        )
        driver = asyncio.create_task(
            self._drive(task_id, state, loop, user_input, state.session_id)
        )
        self._drivers[task_id] = driver
        return state

    async def pause(self, task_id: str) -> dict:
        """请求挂起：当前轮结束后暂停（gate 检查点生效）。"""
        state = self._tasks.get(task_id)
        if state is None:
            raise KeyError(f"任务 {task_id!r} 不存在")
        if state.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise RuntimeError(f"任务已结束 ({state.status.value})")
        state.status = TaskStatus.PAUSED
        state.pause_event.set()
        return {"task_id": task_id, "status": "paused"}

    async def resume(self, task_id: str, *, input_text: str | None = None) -> dict:
        """恢复任务；input_text 非空时先注入人类输入（写入会话树 user 节点）。"""
        state = self._tasks.get(task_id)
        if state is None:
            raise KeyError(f"任务 {task_id!r} 不存在")
        if state.status not in (TaskStatus.PAUSED, TaskStatus.PENDING):
            raise RuntimeError(f"任务不在可恢复状态 ({state.status.value})")
        if input_text:
            state.session_id = state.session_id or (
                self._tree.create_session(system=self._system_prompt or None)
            )
            self._tree.append(state.session_id, role="user", content=input_text)
        state.status = TaskStatus.RUNNING
        state.pause_event.clear()
        state._resume_event.set()
        return {"task_id": task_id, "status": "running"}

    async def status(self, task_id: str) -> dict:
        state = self._tasks.get(task_id)
        if state is None:
            raise KeyError(f"任务 {task_id!r} 不存在")
        return state.to_dict()

    def stream(self, task_id: str) -> AsyncIterator[dict]:
        """订阅任务事件流（agent_loop.* + task.end 经中继按 task 分发）。"""

        async def _gen() -> AsyncIterator[dict]:
            q: asyncio.Queue = asyncio.Queue(maxsize=1024)
            self._fans.setdefault(task_id, []).append(q)
            try:
                while True:
                    event = await q.get()
                    if event.get("type") == "task.end":
                        break
                    yield event
            finally:
                fans = self._fans.get(task_id)
                if fans and q in fans:
                    fans.remove(q)

        return _gen()

    # ------------------------------------------------------------------ 内部

    def _make_gate(self, state: TaskState) -> Callable[[], Awaitable[None]]:
        async def _gate() -> None:
            # paused → 等待 resume 事件（HITL 检查点）
            while state.pause_event.is_set():
                state._resume_event.clear()
                await state._resume_event.wait()

        return _gate

    async def _drive(
        self,
        task_id: str,
        state: TaskState,
        loop: AgentLoop,
        user_input: str,
        session_id: str | None,
    ) -> None:
        """后台驱动器：驱动 AgentLoop 并维护状态机。"""
        if state.status != TaskStatus.PAUSED:  # pause 先于 driver 启动时保持 PAUSED
            state.status = TaskStatus.RUNNING
        try:
            result = await loop.run(user_input, session_id=session_id)
            state.session_id = result.session_id
            state.result = result
            state.status = TaskStatus.COMPLETED
        except asyncio.CancelledError:
            state.status = TaskStatus.FAILED
            state.error = "任务被取消"
            raise
        except Exception as exc:
            state.status = TaskStatus.FAILED
            state.error = str(exc)
        finally:
            # 任务终止信号：经事件流发出（relay 保证顺序在 agent_loop.done 之后）
            emit_event(
                "task.end",
                {"task_id": task_id, "session_id": state.session_id, "status": state.status.value},
                barrier=self._barrier,
            )

    def _end_fan(self, task_id: str) -> None:
        """兼容占位：终止信号已改走 task.end 事件流，保留签名防外部调用。"""
        return None

    async def _relay_loop(self) -> None:
        """中继 barrier 的 agent_loop.* 事件 → 按 session_id 分发给任务队列。"""
        assert self._barrier is not None
        async for event in self._barrier.stream(*_RELAY_TOPICS):
            sid = (event.payload or {}).get("session_id", "")
            if not sid:
                continue
            for task_id, state in self._tasks.items():
                if state.session_id == sid or (not state.session_id and task_id == sid):
                    for q in self._fans.get(task_id, []):
                        with contextlib.suppress(asyncio.QueueFull):
                            q.put_nowait({"type": event.topic, **event.payload})
                    break

    # -- DaemonBus handlers（oprim.daemon 原子直接可用） ---------------------

    async def _on_bus_pause(self, payload: dict, **kw: Any) -> dict:
        task_id = payload.get("session_id") or payload.get("task_id")
        try:
            return await self.pause(task_id)
        except (KeyError, RuntimeError) as exc:
            return {"error": str(exc)}

    async def _on_bus_resume(self, payload: dict, **kw: Any) -> dict:
        task_id = payload.get("session_id") or payload.get("task_id")
        try:
            return await self.resume(task_id, input_text=payload.get("input_text"))
        except (KeyError, RuntimeError) as exc:
            return {"error": str(exc)}

    async def _on_bus_status(self, payload: dict, **kw: Any) -> dict:
        task_id = payload.get("session_id") or payload.get("task_id")
        try:
            return await self.status(task_id)
        except KeyError as exc:
            return {"error": str(exc)}


__all__ = ["DaemonEngine", "TaskState", "TaskStatus"]
