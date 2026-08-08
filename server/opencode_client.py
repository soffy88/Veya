"""opencode serve 常驻客户端 — 免每次 spawn CLI + SSE 真流式。

机制: HTTP 常驻会话 (POST /session + POST /session/{id}/message) 替代每次
spawn opencode CLI; 流式走 SSE (GET /event → message.part.updated delta →
session.idle 终止)。会话按模型缓存复用 (LRU), 失败由调用方回退 CLI 路径。

装配: engine_runner run_engine/stream_engine 的 opencode 分支优先走本客户端。
零新增依赖 (httpx)。协议对齐 @opencode-ai/sdk (1.18.x):
  POST /session                          → {id, ...}
  POST /session/{id}/message             body {parts:[{type:"text",text}],
                                                model:{providerID, modelID}}
  GET  /event (SSE)                      → Event(GlobalEvent) 流
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_PORT = 18765
_SESSION_CACHE_MAX = 8

# 容器内 opencode 二进制 (PATH 无 ~/.opencode/bin)
_OC_BIN_CANDIDATES = (
    os.path.join(os.path.expanduser("~"), ".opencode", "bin", "opencode"),
    shutil.which("opencode") or "",
)


def _oc_bin() -> str | None:
    for c in _OC_BIN_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    return None


def base_url() -> str:
    """常驻 serve 地址: env 覆盖 > 容器内网关(宿主) > 本机。"""
    env = os.environ.get("VEYA_OPENCODE_SERVE_URL", "").strip()
    if env:
        return env.rstrip("/")
    port = int(os.environ.get("VEYA_OPENCODE_SERVE_PORT", DEFAULT_PORT))
    # 容器内优先宿主桥 (引擎代理 env 同款网关)
    if os.environ.get("_IN_CONTAINER") or os.environ.get("PYTHON_CONTAINER"):
        gw = _container_gateway_ip()
        if gw:
            return f"http://{gw}:{port}"
    return f"http://127.0.0.1:{port}"


def _container_gateway_ip() -> str | None:
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return None


# ── 常驻进程确保 (幂等: 探测 → 无则 spawn) ───────────────────────────────
_ensure_lock = asyncio.Lock()
_spawned: list[subprocess.Popen] = []


def ensure_server(timeout_s: float = 10.0) -> bool:
    """探测 serve → 无则 spawn (幂等)。返回是否就绪。"""
    try:
        r = httpx.get(base_url() + "/", timeout=1.5)
        if r.status_code < 500:
            return True
    except Exception:
        pass
    return _spawn_server(timeout_s)


def _spawn_server(timeout_s: float) -> bool:
    """spawn `opencode serve --port <port>` (后台, 会话独立)。"""
    if _spawned:
        return True  # 本次进程已尝试 spawn, 交由探测结果
    bin_path = _oc_bin()
    if not bin_path:
        return False
    port = int(os.environ.get("VEYA_OPENCODE_SERVE_PORT", DEFAULT_PORT))
    try:
        proc = subprocess.Popen(
            [bin_path, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _spawned.append(proc)
    except Exception as exc:
        logger.warning("opencode serve spawn 失败: %s", exc)
        return False
    # 等待就绪
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(base_url() + "/", timeout=1.0).status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


# ── 会话缓存 (model → session_id, LRU) ─────────────────────────────────────
_session_cache: OrderedDict[str, str] = OrderedDict()


def _cache_get(model: str) -> str | None:
    sid = _session_cache.get(model)
    if sid:
        _session_cache.move_to_end(model)
    return sid


def _cache_put(model: str, session_id: str) -> None:
    _session_cache[model] = session_id
    _session_cache.move_to_end(model)
    while len(_session_cache) > _SESSION_CACHE_MAX:
        _session_cache.popitem(last=False)


def _cache_drop(model: str) -> None:
    _session_cache.pop(model, None)


async def get_or_create_session(client: httpx.AsyncClient, model: str) -> str:
    """取缓存会话 / POST /session 创建。"""
    cached = _cache_get(model)
    if cached:
        return cached
    r = await client.post("/session", json={"title": "veya"}, timeout=15.0)
    r.raise_for_status()
    sid = str(r.json().get("id") or "")
    if not sid:
        raise RuntimeError("opencode serve 创建会话失败: 响应无 id")
    _cache_put(model, sid)
    return sid


# ── 消息发送 (非流式: 阻塞到完成) ─────────────────────────────────────────
def _message_body(prompt: str, model: str) -> dict[str, Any]:
    provider_id, _, model_id = model.partition("/")
    if not model_id:
        model_id = model
        provider_id = "opencode-go"
    return {
        "parts": [{"type": "text", "text": prompt}],
        "model": {"providerID": provider_id, "modelID": model_id},
    }


async def send_message(
    prompt: str, model: str, *, timeout_s: float = 30.0
) -> dict[str, Any]:
    """HTTP 常驻会话发送 → 完整回复文本 (阻塞到完成)。失败抛异常。"""
    if not ensure_server():
        raise RuntimeError("opencode serve 不可用 (探测/spawn 失败)")
    async with httpx.AsyncClient(base_url=base_url(), timeout=timeout_s) as client:
        sid = await get_or_create_session(client, model)
        try:
            r = await client.post(
                f"/session/{sid}/message", json=_message_body(prompt, model))
            r.raise_for_status()
        except Exception:
            _cache_drop(model)
            sid = await get_or_create_session(client, model)
            r = await client.post(
                f"/session/{sid}/message", json=_message_body(prompt, model))
            r.raise_for_status()
        data = r.json()
        text = _assistant_text(data)
        return {"ok": True, "output": text, "session_id": sid}


def _assistant_text(data: dict[str, Any]) -> str:
    """POST message 响应 → 拼接 assistant 文本 part。"""
    parts = data.get("parts") or []
    chunks: list[str] = []
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "text":
            chunks.append(str(p.get("text") or ""))
    return "\n".join(c for c in chunks if c)


# ── SSE 真流式 (GET /event → delta) ───────────────────────────────────────
def _parse_sse_data(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


async def stream_send(
    prompt: str, model: str, *, timeout_s: float = 30.0
) -> AsyncIterator[str]:
    """SSE 真流式: 逐 token delta。POST 与 /event 并行, session.idle 终止。

    失败抛异常 → 调用方回退 CLI/非流式路径。
    """
    if not ensure_server():
        raise RuntimeError("opencode serve 不可用 (探测/spawn 失败)")
    async with httpx.AsyncClient(base_url=base_url(), timeout=timeout_s) as client:
        sid = await get_or_create_session(client, model)

        post_task = asyncio.create_task(
            client.post(f"/session/{sid}/message", json=_message_body(prompt, model)))

        saw_idle = False
        yielded = False
        prev_text = ""
        user_msg_id: str | None = None  # 用户消息 id (其 part 是 prompt echo, 不产出)
        try:
            async with client.stream("GET", "/event") as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    evt = _parse_sse_data(line)
                    if not evt:
                        continue
                    # opencode 事件: {id, type, properties: {...}}
                    etype = evt.get("type")
                    props = evt.get("properties") or {}
                    if etype == "message.updated":
                        # 记录用户消息 id, 用于过滤 prompt echo
                        info = props.get("info") or {}
                        if info.get("role") == "user":
                            user_msg_id = str(info.get("id") or "")
                    elif etype == "message.part.updated":
                        part = props.get("part") or {}
                        # part.text = 累计完整文本 (非 delta) → 按增量切片产出
                        if part.get("sessionID") == sid and part.get("type") == "text":
                            text = str(part.get("text") or "")
                            # 跳过用户消息 part (prompt echo) 与空文本
                            if not text or part.get("messageID") == user_msg_id:
                                continue
                            if text == prompt:  # 未捕获 user_msg_id 时的 echo 兜底
                                continue
                            if len(text) > len(prev_text):
                                yielded = True
                                yield text[len(prev_text):]
                                prev_text = text
                    elif etype == "session.idle":
                        if props.get("sessionID") == sid:
                            saw_idle = True
                            break
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            logger.warning("opencode SSE 流中断, 回退完整响应: %s", exc)
        finally:
            # SSE 未产出任何 delta → 用 POST 完整响应兜底 (保证调用方有输出)
            try:
                resp_data = await asyncio.wait_for(post_task, timeout=timeout_s)
                if not saw_idle and not yielded:
                    text = _assistant_text(resp_data.json())
                    if text:
                        yield text
            except Exception:
                pass
