#!/usr/bin/env python3
"""veya LLM 网关 — 正式常驻服务, 独立进程 (不挂进 server/app.py, 零风险)。

把 veya 能打的模型统一暴露成一个 OpenAI 兼容 provider (GET /v1/models +
POST /v1/chat/completions), 外部工具 (pi coding-agent 等) 按 model 字段
自由切换 — 体验对齐直连 opencode-go (同款模型目录, 可换模型), 但换到
veya 自己的别名时额外拿到轮询/兜底能力:

- veya1.2        — 主脑代理: GMI MiniMax M3 优先, OpenRouter 免费模型兜底
- veya1.1        — 兼容旧名称, 实际转发到 veya1.2 主脑代理
- veya1.2-free   — Pi 已配置 provider + Inferera 免费模型轮询
- veya1.2-vl     — openrouter 免费图像/视频理解池轮询 (需 OPENROUTER_API_KEY, 未配则报结构化错误)
- gpt-5.6-luna   — 本地 frontier 直连 (opencodex 桥, 零网络)
- opencode-go/<id> — opencode zen 全量模型直连 (go 按量池 + zen 免费池 -free/big-pickle),
  启动时从 opencode.ai 拉取, 与 opencode-go 官方目录同步

不含 Genesis 专属 NIM key/模型 (与主业务/共享池物理隔离, 见 .env 注释) ——
这个网关服务外部工具, 不该混进 Genesis 专属凭据。

启动: scripts/veya-llm-gateway.sh [PORT]  (默认 8791; systemd --user 常驻见同名 .service)
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from veya.obase._llm_config import get_api_key
from veya.obase.llm import llm_call

app = FastAPI(title="veya LLM gateway")

# ---------------------------------------------------------------------------
# 模型目录: 静态别名/frontier + 启动时从 opencode zen 拉取的全量直连模型
# ---------------------------------------------------------------------------
_STATIC_CATALOG: dict[str, dict[str, Any]] = {
    "veya1.2": {"model": "veya1.2"},
    "veya1.1": {"model": "veya1.1"},
    # 历史别名保留兼容，但不再代表旧的 opencode zen 主脑池。
    "veya1.2-flash": {"model": "veya1.2-flash"},
    "veya1.2-free": {"model": "veya1.2-free"},
    "veya1.2-vl": {"model": "veya1.2-vl"},
    "veya1.2-128K": {"model": "veya1.2-128K"},
    "gpt-5.6-luna": {
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "endpoint": "http://127.0.0.1:10100/v1",
    },
}

# (base_url, 过滤模式): None=go 池全量按量计费直连; "free"=只收 -free/big-pickle
_ZEN_SOURCES = [
    ("https://opencode.ai/zen/go/v1", None),
    ("https://opencode.ai/zen/v1", "free"),
]


def _fetch_zen_catalog() -> dict[str, dict[str, Any]]:
    """启动时拉一次 opencode zen 模型目录 (go 全量 + 通用池 -free/big-pickle)。

    网络失败/无 key 时返回空 (不炸网关, 目录退化为只剩静态别名), 打印警告。
    """
    key = get_api_key("opencode-go")
    catalog: dict[str, dict[str, Any]] = {}
    if not key:
        print("[veya-llm-gateway] 警告: 无 opencode-go key, 跳过 zen 目录拉取", file=sys.stderr)
        return catalog
    for base, mode in _ZEN_SOURCES:
        try:
            resp = httpx.get(
                f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=10.0
            )
            resp.raise_for_status()
            ids = [m["id"] for m in resp.json().get("data", [])]
        except Exception as exc:  # 目录拉取失败不阻塞启动
            print(f"[veya-llm-gateway] 警告: 拉取 {base}/models 失败: {exc}", file=sys.stderr)
            continue
        for mid in ids:
            if mode == "free" and not (mid.endswith("-free") or mid == "big-pickle"):
                continue
            catalog[f"opencode-go/{mid}"] = {
                "provider": "opencode-go",
                "model": mid,
                "endpoint": base,
            }
    return catalog


_CATALOG: dict[str, dict[str, Any]] = {**_STATIC_CATALOG, **_fetch_zen_catalog()}
print(f"[veya-llm-gateway] 模型目录就绪: {len(_CATALOG)} 个", file=sys.stderr)


def _pi_provider_config() -> dict[str, Any]:
    """把 Pi 本地 provider keys 转成 llm_call 可用的最小 config。

    仅供本机 Pi → veya 网关链路使用；``!command`` 形式的动态 key 由
    Veya 自己的环境/凭据链处理，不当作字面 API key 传给上游。
    """
    path = Path.home() / ".pi" / "agent" / "models.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    providers: dict[str, dict[str, str]] = {}
    for name, entry in (data.get("providers") or {}).items():
        if not isinstance(entry, dict):
            continue
        api_key = entry.get("apiKey")
        if isinstance(api_key, str) and api_key and not api_key.startswith("!"):
            providers[str(name)] = {"api_key": api_key}
    return {"providers": providers} if providers else {}


_PI_PROVIDER_CONFIG = _pi_provider_config()


async def _sse_from_resp(
    resp: dict[str, Any], resp_id: str, created: int, model: str
) -> AsyncIterator[str]:
    """把 llm_call() 的一次性结果包成 OpenAI 流式 chunk (单帧 delta + 收尾)。

    veya 的路由/重试在 llm_call 内部已经跑完 (非流式), 这里只是套 SSE 外壳 —
    pi 等 openai-completions 客户端默认按流式协议解析, 收不到 finish_reason
    会报「Stream ended without finish_reason」。
    """
    msg = (resp.get("choices") or [{}])[0].get("message") or {}
    delta: dict[str, Any] = {"role": "assistant"}
    if msg.get("content"):
        delta["content"] = msg["content"]
    if msg.get("tool_calls"):
        delta["tool_calls"] = msg["tool_calls"]
    finish_reason = "tool_calls" if msg.get("tool_calls") else "stop"

    def _chunk(choice_delta: dict[str, Any], reason: str | None) -> str:
        payload = {
            "id": resp_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": choice_delta, "finish_reason": reason}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield _chunk(delta, None)
    yield _chunk({}, finish_reason)
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> StreamingResponse | JSONResponse:
    body: dict[str, Any] = await request.json()
    requested = body.get("model") or "veya1.2"
    entry = _CATALOG.get(requested)
    if entry is None:
        return JSONResponse(
            {"error": {"message": f"unknown model: {requested!r}", "type": "invalid_request"}},
            status_code=400,
        )
    messages = body.get("messages") or []
    tools = body.get("tools")
    call_kwargs: dict[str, Any] = {"model": entry["model"], "tools": tools}
    if entry.get("provider"):
        call_kwargs["provider"] = entry["provider"]
    if entry.get("endpoint"):
        call_kwargs["endpoint"] = entry["endpoint"]
    if requested == "veya1.2-free" and _PI_PROVIDER_CONFIG:
        call_kwargs["config"] = _PI_PROVIDER_CONFIG
    resp = await llm_call(messages, **call_kwargs)
    resp_id = f"{requested}-{int(time.time() * 1000)}"
    created = int(time.time())
    if body.get("stream"):
        return StreamingResponse(
            _sse_from_resp(resp, resp_id, created, requested), media_type="text/event-stream"
        )
    resp.setdefault("id", resp_id)
    resp.setdefault("object", "chat.completion")
    resp.setdefault("created", created)
    resp.setdefault("model", requested)
    return JSONResponse(resp)


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {"id": mid, "object": "model", "owned_by": "veya"} for mid in sorted(_CATALOG)
            ],
        }
    )


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
