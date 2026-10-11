"""server/acp_client.py — ACP (Agent Client Protocol) 客户端（ACP 语义层）。

对标 OpenHands "任何 ACP 兼容 agent 均可挂载" —— 让 Veya 作为控制中心
统一运行外部 ACP agent (OpenHands / 其他实现 agents.md 协议的工具)。

协议: JSON-RPC 2.0 over stdio (agent 进程 stdin/stdout)。
本客户端实现 ACP 子集:
  session/new → session/init → task/start → 收集 task/event 通知 → task/cancel/session/close

**单源约定 (§1.4)**: 字节层 stdio JSON-RPC 传输的唯一实现是
``veya/obase/acp_stdio.py::StdioJsonRpcTransport`` (obase = 外部适配层)。
本模块只保留 **ACP 语义** (方法名、会话、task 事件聚合), 传输经注入委托,
不再自带一份 process/IO 实现。守卫测试见 tests/test_nano_w9_buzz.py::
``TestAcpStdioSingleSource``。

用法:
    backend = ACPBackend(command=["my-acp-agent"])
    result = await backend.run("修复登录页 bug", agent="general", timeout_s=300)
    await backend.close()
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from veya.obase.acp_stdio import StdioJsonRpcError, StdioJsonRpcTransport


class ACPError(RuntimeError):
    """ACP 协议错误 (进程退出 / JSON-RPC error / 超时)。"""


class ACPBackend:
    """ACP agent 客户端: ACP 语义 + 注入的 stdio 传输。

    Args:
        command: 被 spawn 的 ACP agent argv。
        agent: 报告给 agent 的身份标签。
        cwd / env: 子进程工作目录与环境。
        transport: 注入的传输实例 (默认构造一个 canonical StdioJsonRpcTransport)。
    """

    def __init__(
        self,
        command: list[str],
        *,
        agent: str = "general",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        transport: StdioJsonRpcTransport | None = None,
    ) -> None:
        self.command = command
        self.agent = agent
        self.cwd = cwd
        self.env = env
        self._session_id: str | None = None
        self._event_log: list[dict[str, Any]] = []
        self._transport: StdioJsonRpcTransport | None = transport
        self._owned_transport = transport is None

    # ── 传输 (obase 适配层, 非本模块实现) ─────────────────────────────────
    @property
    def transport(self) -> StdioJsonRpcTransport:
        """返回 canonical 传输; 首次访问时按 ACPBackend 的参数构造。"""
        if self._transport is None:
            self._transport = StdioJsonRpcTransport(
                self.command,
                cwd=self.cwd,
                env=self.env,
                on_notification=self._on_notification,
                name="acp",
            )
        return self._transport

    def _on_notification(self, params: dict[str, Any]) -> None:
        if params.get("method") == "task/event" or "event" in params:
            self._event_log.append(params)

    async def _request(
        self, method: str, params: dict[str, Any], timeout_s: float = 30.0
    ) -> dict[str, Any]:
        """委托给 canonical 传输, 把传输错误翻成 ACPError。"""
        try:
            return await self.transport.request(method, params, timeout_s=timeout_s)
        except StdioJsonRpcError as e:
            raise ACPError(str(e)) from e

    # ── ACP 会话 ───────────────────────────────────────────────────────
    async def start_session(self, timeout_s: float = 30.0) -> str:
        """session/new + session/init → 返回 sessionId。"""
        res = await self._request("session/new", {"metadata": {}}, timeout_s)
        sid = res.get("sessionId", "")
        if not sid:
            raise ACPError("session/new 未返回 sessionId")
        await self._request(
            "session/init",
            {
                "sessionId": sid,
                "agent": self.agent,
                "capabilities": {"text": True, "file": False, "audio": False},
            },
            timeout_s,
        )
        self._session_id = sid
        return sid

    async def run(
        self, prompt: str, *, timeout_s: float = 600.0, task_id: str | None = None
    ) -> dict[str, Any]:
        """完整任务: 建会话 → task/start → 等完成/超时 → 聚合文本。"""
        t0 = time.time()
        if self._session_id is None:
            await self.start_session()
        assert self._session_id is not None
        self._event_log.clear()

        tid = task_id or f"veya-{int(t0)}"
        await self._request(
            "task/start",
            {
                "sessionId": self._session_id,
                "prompt": prompt,
                "taskId": tid,
            },
            timeout_s=30.0,
        )

        # 等待: 事件流出现 task/end 或超时
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            for evt in self._event_log:
                event = evt.get("event", {})
                if isinstance(event, dict) and event.get("type") == "task/end":
                    return self._collect_result(tid)
            await asyncio.sleep(0.2)
        await self.cancel(tid)
        raise ACPError(f"ACP 任务超时 ({timeout_s:.0f}s)")

    def _collect_result(self, task_id: str) -> dict[str, Any]:
        texts: list[str] = []
        for evt in self._event_log:
            event = evt.get("event") or {}
            if not isinstance(event, dict) or event.get("type") != "text":
                continue
            content = event.get("content")
            if isinstance(content, dict):
                texts.append(str(content.get("text", "")))
            elif isinstance(content, str):
                texts.append(content)
        return {
            "ok": True,
            "task_id": task_id,
            "output": "\n".join(t for t in texts if t),
            "events": len(self._event_log),
        }

    async def cancel(self, task_id: str | None = None) -> None:
        if self._session_id is None:
            return
        with contextlib.suppress(ACPError):
            await self._request(
                "task/cancel",
                {
                    "sessionId": self._session_id,
                    "taskId": task_id or "",
                },
                timeout_s=5.0,
            )

    async def close(self) -> None:
        """session/close + 终止进程。"""
        if self._session_id is not None:
            with contextlib.suppress(ACPError):
                await self._request("session/close", {"sessionId": self._session_id}, timeout_s=5.0)
            self._session_id = None
        if self._transport is not None:
            if self._owned_transport:
                await self._transport.close()
            self._transport = None


__all__ = ["ACPBackend", "ACPError"]