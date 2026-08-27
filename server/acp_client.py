"""server/acp_client.py — ACP (Agent Client Protocol) 最小客户端。

对标 OpenHands "任何 ACP 兼容 agent 均可挂载" —— 让 Veya 作为控制中心
统一运行外部 ACP agent (OpenHands / 其他实现 agents.md 协议的工具)。

协议: JSON-RPC 2.0 over stdio (agent 进程 stdin/stdout)。
本客户端实现 ACP 子集:
  session/new → session/init → task/start → 收集 task/event 通知 → task/cancel/session/close

用法:
    backend = ACPBackend(command=["my-acp-agent"])
    result = await backend.run("修复登录页 bug", agent="general", timeout_s=300)
    await backend.close()
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

_ID_COUNTER = 0


def _next_id() -> int:
    global _ID_COUNTER
    _ID_COUNTER += 1
    return _ID_COUNTER


class ACPError(RuntimeError):
    """ACP 协议错误 (进程退出 / JSON-RPC error / 超时)。"""


class ACPBackend:
    """ACP agent 客户端: 管理子进程 + JSON-RPC 会话 + 任务事件收集。"""

    def __init__(
        self,
        command: list[str],
        *,
        agent: str = "general",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.agent = agent
        self.cwd = cwd
        self.env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._session_id: str | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._event_log: list[dict[str, Any]] = []
        self._writer: asyncio.StreamWriter | None = None

    # ── 进程/IO ────────────────────────────────────────────────────────
    async def _ensure_proc(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=self.cwd,
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise ACPError(f"ACP 进程启动失败: {e}") from e
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """逐行读 stdout, 分派响应/通知。"""
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode(errors="replace"))
            except json.JSONDecodeError:
                continue
            if msg.get("method") == "task/event":
                self._event_log.append(msg.get("params") or {})
                continue
            rid = msg.get("id")
            if rid is not None and rid in self._pending:
                fut = self._pending.pop(rid)
                if "error" in msg:
                    fut.set_exception(ACPError(f"ACP error: {msg['error']}"))
                else:
                    fut.set_result(msg.get("result") or {})
        # 进程退出: 未决请求全部失败
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ACPError("ACP 进程已退出"))
        self._pending.clear()

    async def _request(
        self, method: str, params: dict[str, Any], timeout_s: float = 30.0
    ) -> dict[str, Any]:
        await self._ensure_proc()
        assert self._proc is not None and self._proc.stdin is not None
        rid = _next_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self._proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self._proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except TimeoutError:
            self._pending.pop(rid, None)
            raise ACPError(f"ACP 请求超时: {method}") from None

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
                if evt.get("event", {}).get("type") == "task/end":
                    return self._collect_result(tid)
            await asyncio.sleep(0.2)
        await self.cancel(tid)
        raise ACPError(f"ACP 任务超时 ({timeout_s:.0f}s)")

    def _collect_result(self, task_id: str) -> dict[str, Any]:
        texts: list[str] = []
        for evt in self._event_log:
            event = evt.get("event") or {}
            if event.get("type") == "text":
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
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._proc is not None:
            try:
                self._proc.kill()
                await self._proc.wait()
            except ProcessLookupError:
                pass
            self._proc = None


__all__ = ["ACPBackend", "ACPError"]
