"""server.hicode_serve — Hicode 独立 oservi (HTTP+SSE) 客户端。

veya 编程任务主流流程: 主脑理解 → 规范指令 → 本客户端 → hicode serve
(独立进程, 容器内 :8768 / 宿主 :8768, opencode-go 云端 key, 与 veya 网关解耦)。

serve 是 Hicode 的完整交互会话 (浏览器 UI 后端):
  POST /submit /cancel /approve /plan /goal /rewind /fork /compact /new
  GET  /events (SSE: turn_started / tool_dispatch / tool_result / message /
                 usage / phase / approval_request / turn_done 终态)

约束: serve 单会话 → 本客户端用 asyncio.Lock 串行化任务 (编程任务低频, 可接受)。
事件映射复用 hicode_progress 前端格式 (stage/tool/detail)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("hicode.serve")

SERVE_BASE = os.environ.get("HICODE_SERVE_BASE", "http://127.0.0.1:8768")
SERVE_TASK_TIMEOUT = float(os.environ.get("HICODE_SERVE_TIMEOUT", "1800"))


class HicodeServeError(RuntimeError):
    """serve 端点错误 / 协议异常 (主脑应看到可操作提示)。"""


class HicodeServeClient:
    """hicode serve 独立 oservi 客户端 (单会话 → 任务串行锁)。"""

    def __init__(self, base: str = SERVE_BASE) -> None:
        self.base = base.rstrip("/")
        self._lock = asyncio.Lock()

    # ── 端点 ──────────────────────────────────────────────────────────
    async def _post(self, path: str, payload: dict | None = None) -> httpx.Response:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.base}{path}", json=payload or {})
            if r.status_code >= 400:
                raise HicodeServeError(f"POST {path} → {r.status_code}: {r.text[:200]}")
            return r

    async def submit(self, spec: str) -> None:
        await self._post("/submit", {"input": spec, "format": "json_object"})

    async def cancel(self) -> None:
        """软中断: 请求 serve 停当前 turn。

        注意: 对运行中的模型调用 (如 opencode-go 云端) 不保证立即生效,
        turn 可能继续跑到本轮结束。硬停止用 restart_serve。
        """
        await self._post("/cancel")

    async def restart_serve(self, wait_s: float = 40.0) -> bool:
        """硬停止: kill serve 进程 (守护循环自动重启), 等待恢复健康。

        真正中断运行中的 turn (模型调用随之断开)。重启 ~2s (守护循环)。
        返回是否恢复健康。
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "pkill",
                "-f",
                "hicode serve",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception as exc:  # noqa: BLE001
            logger.warning("restart_serve: pkill 失败: %s", exc)
        deadline = asyncio.get_event_loop().time() + wait_s
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1.5)
            if await self.health():
                return True
        return False

    async def approve(self, approval_id: str, allow: bool = True) -> None:
        await self._post(
            "/approve",
            {"id": approval_id, "allow": allow, "session": False, "persist": False},
        )

    async def set_approval_mode(self, mode: str = "auto") -> None:
        await self._post("/tool-approval-mode", {"mode": mode})

    async def plan_mode(self, on: bool) -> None:
        await self._post("/plan", {"on": on})

    async def set_goal(self, goal: str) -> None:
        await self._post("/goal", {"goal": goal})

    async def new_session(self) -> None:
        await self._post("/new")

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.base}/")
            return r.status_code == 200
        except Exception:
            return False

    # ── SSE 事件流 ────────────────────────────────────────────────────
    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """订阅 /events SSE 流, 逐事件产出 (不含心跳注释行)。"""
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("GET", f"{self.base}/events") as r:
                if r.status_code != 200:
                    raise HicodeServeError(f"GET /events → {r.status_code}")
                buf = ""
                async for chunk in r.aiter_text():
                    buf += chunk
                    while "\n\n" in buf:
                        frame, buf = buf.split("\n\n", 1)
                        for line in frame.split("\n"):
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if not data:
                                continue
                            try:
                                yield json.loads(data)
                            except json.JSONDecodeError:
                                continue

    # ── 任务执行 (串行) ───────────────────────────────────────────────
    async def run_task(
        self,
        spec: str,
        on_event: Callable[[dict], None] | None = None,
        *,
        approve_all: bool = True,
        timeout: float = SERVE_TASK_TIMEOUT,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """执行一次编程任务。同 workspace 互斥；全局/每用户槽位由 SandboxBroker。"""
        if not await self.health():
            return {
                "status": "error",
                "error": (
                    "hicode serve 不可达 (127.0.0.1:8768)。容器内由启动脚本拉起, "
                    "宿主需 `nohup hicode serve --addr 127.0.0.1:8768 --auth none "
                    "--model opencode-go &` 启动。"
                ),
            }
        from veya.platform import load

        broker = load("omodul").get_broker()
        try:
            from server.auth import current_user

            owner_id = str(current_user().get("user_id") or "")
        except Exception:
            owner_id = ""
        async with (
            broker.async_workspace(workspace),
            broker.async_slot("hicode_serve", owner_id=owner_id),
        ):
            return await self._run_task_locked(
                spec, on_event, approve_all=approve_all, timeout=timeout
            )

    async def _run_task_locked(
        self,
        spec: str,
        on_event: Callable[[dict], None] | None,
        *,
        approve_all: bool,
        timeout: float,
    ) -> dict[str, Any]:
        async with self._lock:
            await self.new_session()
            await self.set_approval_mode("auto" if approve_all else "ask")
            if on_event is not None:
                on_event(
                    {"stage": "planning", "tool": None, "detail": "Hicode oservi 已就绪, 提交任务…"}
                )

            q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

            async def _drain() -> None:
                try:
                    async for ev in self.events():
                        await q.put(ev)
                except Exception as exc:  # noqa: BLE001 — 流断 → 终止
                    await q.put({"kind": "turn_done", "err": f"events 断开: {exc}"})
                finally:
                    await q.put(None)

            drain = asyncio.create_task(_drain())
            try:
                await asyncio.sleep(0.6)  # 等 SSE 连接建立
                await self.submit(spec)
                text_parts: list[str] = []
                tool_calls: list[dict] = []
                turns = 0
                usage_total: dict[str, int] = {}
                while True:
                    ev = await asyncio.wait_for(q.get(), timeout=timeout)
                    if ev is None:
                        break  # 流结束(未收 turn_done → 视为异常)
                    kind = ev.get("kind")
                    if kind == "turn_done":
                        err = ev.get("err") or ev.get("error") or ""
                        result = "".join(text_parts).strip()
                        if err:
                            return {
                                "status": "error",
                                "error": str(err),
                                "result": result or None,
                                "turns": turns,
                                "tool_calls": tool_calls,
                            }
                        return {
                            "status": "success",
                            "result": result,
                            "turns": turns,
                            "tool_calls": tool_calls,
                            "usage": usage_total,
                        }
                    _bridge_event(ev, on_event)
                    if kind == "turn_started":
                        turns += 1
                    elif kind == "text":
                        t = ev.get("text") or ""
                        if isinstance(t, str) and t:
                            text_parts.append(t)
                    elif kind == "tool_dispatch":
                        tool = ev.get("tool") or {}
                        tc = {
                            "tool": tool.get("name"),
                            "args": tool.get("args"),
                            "read_only": tool.get("readOnly"),
                        }
                        tool_calls.append(tc)
                    elif kind == "usage":
                        u = ev.get("usage") or {}
                        for k in ("promptTokens", "completionTokens"):
                            if u.get(k):
                                usage_total[k] = usage_total.get(k, 0) + u[k]
                return {
                    "status": "error",
                    "error": "hicode 会话意外结束 (未收到 turn_done)",
                    "result": "".join(text_parts).strip() or None,
                    "turns": turns,
                }
            finally:
                drain.cancel()


def _bridge_event(ev: dict, on_event: Callable[[dict], None] | None) -> None:
    """serve 事件 → hicode_progress 前端格式 (stage/tool/detail)。"""
    if on_event is None:
        return
    kind = ev.get("kind")
    if kind == "turn_started":
        on_event({"stage": "planning", "tool": None, "detail": "Hicode 规划中…"})
    elif kind == "phase":
        label = str(ev.get("text") or ev.get("label") or "交接")
        on_event({"stage": "planning", "tool": None, "detail": f"阶段: {label}"})
    elif kind == "tool_dispatch":
        tool = ev.get("tool") or {}
        name = str(tool.get("name") or "tool")
        args = tool.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        on_event({"stage": "executing", "tool": name, "detail": _tool_brief(name, args)})
    elif kind == "tool_result":
        tool = ev.get("tool") or {}
        name = str(tool.get("name") or "tool")
        ms = tool.get("durationMs")
        on_event(
            {
                "stage": "executing",
                "tool": name,
                "detail": f"{name} 完成" + (f" ({ms}ms)" if ms else ""),
            }
        )
    elif kind == "tool_progress":
        tool = ev.get("tool") or {}
        out = (tool.get("output") or "")[-40:]
        on_event({"stage": "executing", "tool": tool.get("name") or "tool", "detail": out})
    elif kind == "usage":
        u = ev.get("usage") or {}
        pt, ct = u.get("promptTokens"), u.get("completionTokens")
        if pt or ct:
            on_event({"stage": "stats", "tool": None, "detail": f"tokens: in={pt} out={ct}"})
    elif kind == "compaction_started":
        on_event({"stage": "planning", "tool": None, "detail": "上下文压缩中…"})
    elif kind == "approval_request":
        ap = ev.get("approval") or {}
        on_event(
            {
                "stage": "executing",
                "tool": ap.get("tool") or "tool",
                "detail": f"等待审批: {ap.get('subject') or ap.get('id')}",
            }
        )


def _tool_brief(name: str, args: dict) -> str:
    """工具调用摘要 (进度徽章用, 截断防 SSE 帧膨胀)。"""
    try:
        if name in ("write_file", "create_file", "edit_file", "patch"):
            p = args.get("path") or args.get("file_path") or ""
            fn = Path(p).name or p
            content = args.get("content") or ""
            return f"写入 {fn}" + (f" ({len(str(content))}B)" if content else "")
        if name in ("bash", "terminal", "run_command", "run"):
            return f"运行: {str(args.get('command') or args.get('cmd') or '')[:80]}"
        if name in ("search", "grep", "glob"):
            return f"搜索: {str(args.get('query') or args.get('pattern') or '')[:60]}"
        if name in ("read", "read_file"):
            return f"读 {Path(str(args.get('path') or '')).name}"
        brief = json.dumps(args, ensure_ascii=False)
        return f"{name}: {brief[:80]}"
    except Exception:
        return name


# 单例客户端 (服务级复用)
_serve_client: HicodeServeClient | None = None


def get_serve_client() -> HicodeServeClient:
    global _serve_client
    if _serve_client is None:
        _serve_client = HicodeServeClient()
    return _serve_client
