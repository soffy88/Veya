#!/usr/bin/env python3
"""veya LLM 网关 — 正式常驻服务, 独立进程 (不挂进 server/app.py, 零风险)。

把 veya 能打的模型统一暴露成一个 OpenAI 兼容 provider (GET /v1/models +
POST /v1/chat/completions), 外部工具 (pi coding-agent 等) 按 model 字段
自由切换 — 体验对齐直连 opencode-go (同款模型目录, 可换模型), 但换到
veya 自己的别名时额外拿到轮询/兜底能力:

- veya1.2        — 主脑代理: GMI MiniMax M3 优先, OpenRouter 免费模型兜底
- veya1.1        — 兼容旧名称, 实际转发到 veya1.2 主脑代理
- veya1.2-free   — Pi 已配置 provider + 已验证免费模型轮询
- veya1.2-vl     — openrouter 免费图像/视频理解池轮询 (需 OPENROUTER_API_KEY, 未配则报结构化错误)
- gpt-5.6-luna   — 本地 frontier 直连 (opencodex 桥, 零网络)
- opencode-go/<id> — opencode zen 全量模型直连 (go 按量池 + zen 免费池 -free/big-pickle),
  启动时从 opencode.ai 拉取, 与 opencode-go 官方目录同步

不含 Genesis 专属 NIM key/模型 (与主业务/共享池物理隔离, 见 .env 注释) ——
这个网关服务外部工具, 不该混进 Genesis 专属凭据。

启动: scripts/veya-llm-gateway.sh [PORT]  (默认 8791; systemd --user 常驻见同名 .service)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from veya.obase._llm_config import get_api_key
from veya.obase import llm as _llm
from veya.obase.free_pool import FreePoolLifecycle, FreePoolSnapshot, entry_key
from veya.obase.llm import _NVIDIA_NIM_ALIASES, llm_call

@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    """Own the daily free-pool task for the whole gateway process lifetime."""
    global _FREE_POOL_TASK
    _FREE_POOL_TASK = asyncio.create_task(_free_pool_loop(), name="veya-free-pool-lifecycle")
    try:
        yield
    finally:
        if _FREE_POOL_TASK is not None:
            _FREE_POOL_TASK.cancel()
            await asyncio.gather(_FREE_POOL_TASK, return_exceptions=True)
            _FREE_POOL_TASK = None


app = FastAPI(title="veya LLM gateway", lifespan=_app_lifespan)

# ---------------------------------------------------------------------------
# 模型目录: 静态别名/frontier + 启动时从 opencode zen 拉取的全量直连模型。
# veya1.2-free 的活动池由下方生命周期任务独立维护。
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
# Keep the external gateway catalog aligned with the aliases accepted by the
# core Veya LLM facade.  Pi reaches this gateway, so aliases absent here are
# rejected before llm_call() can perform the NIM key rotation.
_STATIC_CATALOG.update({alias: {"model": alias} for alias in _NVIDIA_NIM_ALIASES})

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


# ---------------------------------------------------------------------------
# 免费池生命周期: 每日目录发现 + 有界探活 + 持久化状态
# ---------------------------------------------------------------------------
def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


_FREE_POOL_REFRESH_SECONDS = _env_float("VEYA_FREE_POOL_REFRESH_HOURS", 24.0) * 3600
_FREE_POOL_REFRESH_TIMEOUT_SECONDS = _env_float(
    "VEYA_FREE_POOL_REFRESH_TIMEOUT_SECONDS", 180.0
)
_FREE_POOL_FAILURE_THRESHOLD = max(
    1, int(os.environ.get("VEYA_FREE_POOL_FAILURE_THRESHOLD", "3"))
)
_FREE_POOL_MAX_ACTIVE = max(0, int(os.environ.get("VEYA_FREE_POOL_MAX_ACTIVE", "32")))
_FREE_POOL_MAX_DISCOVERED_PER_SOURCE = max(
    1, int(os.environ.get("VEYA_FREE_POOL_MAX_DISCOVERED_PER_SOURCE", "24"))
)
_FREE_POOL_STATE_PATH = os.environ.get("VEYA_FREE_POOL_STATE", "").strip() or str(
    Path.home() / ".veya" / "free-pool-state.json"
)

_FREE_POOL_LIFECYCLE = FreePoolLifecycle(
    _llm._VEYA12_FREE_POOL,
    state_path=_FREE_POOL_STATE_PATH,
    failure_threshold=_FREE_POOL_FAILURE_THRESHOLD,
    max_active=_FREE_POOL_MAX_ACTIVE,
)
# Reuse the last known-good routes immediately after a restart.  The daily
# reconciliation will atomically replace them once the fresh catalog/probes
# complete, so a slow upstream cannot cause a startup routing gap.
if _FREE_POOL_LIFECYCLE.active_pool:
    _llm._replace_veya12_free_pool(_FREE_POOL_LIFECYCLE.active_pool)
_FREE_POOL_TASK: asyncio.Task | None = None
_FREE_POOL_RUNTIME: dict[str, Any] = {
    "refresh_interval_seconds": _FREE_POOL_REFRESH_SECONDS,
    "refresh_timeout_seconds": _FREE_POOL_REFRESH_TIMEOUT_SECONDS,
    "refresh_in_progress": False,
    "last_refresh_ok": False,
    "last_refresh_error": "not started",
}


def _provider_key(provider: str) -> str:
    """Resolve aliases used by the free-pool seed list to configured keys."""
    aliases = {
        "gmi-serving": ("gmi-serving", "gmi"),
        "bai": ("bai",),
        "opencode-go": ("opencode-go",),
        "openrouter": ("openrouter",),
        "inferera": ("inferera",),
        "flatkey": ("flatkey",),
    }
    for candidate in aliases.get(provider, (provider,)):
        key = get_api_key(candidate, _PI_PROVIDER_CONFIG)
        if key:
            return key
    return ""


def _is_free_text_model(model: dict[str, Any]) -> bool:
    """Accept explicit free ids or zero-priced text-chat catalog entries."""
    model_id = str(model.get("id") or "").lower()
    if model_id.endswith(("-free", ":free")) or model_id == "big-pickle":
        free = True
    else:
        pricing = model.get("pricing") or {}
        free = False
        if isinstance(pricing, dict) and pricing:
            values = [pricing.get("prompt"), pricing.get("completion")]
            present = [value for value in values if value is not None]
            if present:
                try:
                    free = all(float(value or 0) == 0 for value in present)
                except (TypeError, ValueError):
                    free = False
        free = bool(model.get("free") or model.get("is_free") or free)
    if not free:
        return False
    architecture = model.get("architecture") or {}
    modalities = architecture.get("input_modalities")
    return not modalities or "text" in modalities


async def _fetch_catalog(
    client: httpx.AsyncClient,
    *,
    source: str,
    base: str,
) -> tuple[bool, list[dict[str, Any]], str]:
    key = _provider_key(source)
    if not key:
        return False, [], "API key not configured"
    try:
        response = await client.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        response.raise_for_status()
        payload = response.json()
        models = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(models, list):
            return False, [], "invalid models response"
        return True, [model for model in models if isinstance(model, dict)], ""
    except Exception as exc:
        return False, [], f"{type(exc).__name__}: {exc}"


async def _discover_free_pool() -> FreePoolSnapshot:
    """Discover free text candidates and complete source catalogs."""
    sources = [
        ("opencode-go", "https://opencode.ai/zen/v1"),
        ("opencode-go", "https://opencode.ai/zen/go/v1"),
        ("openrouter", "https://openrouter.ai/api/v1"),
        ("inferera", "https://api.inferera.com/v1"),
        ("bai", "https://api.b.ai/v1"),
        ("gmi-serving", "https://api.gmi-serving.com/v1"),
    ]
    available: dict[str, set[str]] = {}
    entries: dict[str, dict[str, str]] = {}
    healthy: set[str] = set()
    errors: dict[str, str] = {}
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
    timeout = httpx.Timeout(12.0, connect=5.0, write=5.0, pool=5.0)
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        results = await asyncio.gather(
            *(_fetch_catalog(client, source=source, base=base) for source, base in sources)
        )
    per_source_count: dict[str, int] = {}
    for (source, base), (ok, models, error) in zip(sources, results, strict=True):
        if not ok:
            errors[source] = error
            continue
        healthy.add(source)
        ids = available.setdefault(source, set())
        for model in models:
            model_id = str(model.get("id") or "").strip()
            if not model_id:
                continue
            ids.add(model_id.lower())
            if not _is_free_text_model(model):
                continue
            if per_source_count.get(source, 0) >= _FREE_POOL_MAX_DISCOVERED_PER_SOURCE:
                continue
            if source == "opencode-go":
                entry = {
                    "provider": "opencode-go",
                    "model": model_id,
                    "endpoint": base,
                    "source": source,
                }
            else:
                endpoint = {
                    "openrouter": "https://openrouter.ai/api/v1",
                    "inferera": "https://api.inferera.com/v1",
                    "bai": "https://api.b.ai/v1",
                    "gmi-serving": "https://api.gmi-serving.com/v1",
                }[source]
                entry = {
                    "provider": source,
                    "model": model_id,
                    "endpoint": endpoint,
                    "source": source,
                }
            key = entry_key(entry)
            if key not in entries:
                entries[key] = entry
                per_source_count[source] = per_source_count.get(source, 0) + 1
    return FreePoolSnapshot(
        entries=tuple(entries.values()),
        available={source: frozenset(ids) for source, ids in available.items()},
        healthy_sources=frozenset(healthy),
        errors=errors,
    )


async def _probe_free_entry(client: httpx.AsyncClient, entry: dict[str, str]) -> tuple[bool, str]:
    """Run a bounded, text-only probe without constructing a client per model."""
    source = entry.get("source") or entry.get("provider") or ""
    provider = "opencode-go" if source == "opencode-go" else entry.get("provider", "")
    model = entry.get("model", "")
    if source == "opencode-go" and model.startswith("opencode-go/"):
        model = model.split("/", 1)[1]
    endpoint = entry.get("endpoint", "")
    if source == "opencode-go":
        # The original seed used the local gateway endpoint.  Lifecycle probes
        # must bypass that gateway to avoid probing the service recursively.
        endpoint = "https://opencode.ai/zen/v1"
    key = _provider_key(provider)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 128,
        "temperature": 0,
        "stream": False,
    }
    try:
        chat_endpoint = endpoint.rstrip("/")
        if not chat_endpoint.endswith("/chat/completions"):
            chat_endpoint += "/chat/completions"
        response = await client.post(chat_endpoint, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        message = ((data.get("choices") or [{}])[0].get("message") or {})
        if message.get("tool_calls") or str(message.get("content") or "").strip():
            return True, ""
        return False, "empty response"
    except Exception as exc:
        detail = str(exc).replace("\n", " ")[:300]
        return False, f"{type(exc).__name__}: {detail}"


async def _refresh_free_pool_once() -> None:
    _FREE_POOL_RUNTIME["refresh_in_progress"] = True
    try:
        snapshot = await _discover_free_pool()
        # max_connections=4 与 free_pool.reconcile 的 BATCH_SIZE=8 配合: 每批
        # 8 个探测请求被 httpx 池里 4 个连接复用, 峰值内存约 ~100MB (之前 8
        # 个连接并发 + gather 50+ 探测 → 峰值 ~400MB → 被 oomd kill)。
        limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
        # More probe coroutines than connections are intentional.  A generous
        # pool wait prevents queued models from being mislabeled unhealthy merely
        # because they were behind the first eight probes.
        timeout = httpx.Timeout(15.0, connect=5.0, write=5.0, pool=120.0)
        async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
            active = await _FREE_POOL_LIFECYCLE.reconcile(
                snapshot,
                lambda entry: _probe_free_entry(client, entry),
            )
        _llm._replace_veya12_free_pool(active)
        _FREE_POOL_RUNTIME.update(
            {
                "last_refresh_ok": True,
                "last_refresh_error": _FREE_POOL_LIFECYCLE.last_error,
                "active_count": len(active),
                "discovered_count": len(snapshot.entries),
                "healthy_sources": sorted(snapshot.healthy_sources),
                "next_refresh_in_seconds": _FREE_POOL_REFRESH_SECONDS,
            }
        )
        print(
            f"[veya-llm-gateway] 免费池刷新: active={len(active)} discovered={len(snapshot.entries)} "
            f"sources={','.join(sorted(snapshot.healthy_sources))}",
            file=sys.stderr,
        )
    finally:
        _FREE_POOL_RUNTIME["refresh_in_progress"] = False


async def _free_pool_loop() -> None:
    while True:
        try:
            await asyncio.wait_for(
                _refresh_free_pool_once(), timeout=_FREE_POOL_REFRESH_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _FREE_POOL_RUNTIME.update(
                {"last_refresh_ok": False, "last_refresh_error": f"{type(exc).__name__}: {exc}"}
            )
            print(f"[veya-llm-gateway] 免费池刷新失败: {exc}", file=sys.stderr)
        await asyncio.sleep(_FREE_POOL_REFRESH_SECONDS)


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
    # Model ids in Pi are human-readable/case-preserving; Veya aliases are
    # normalized to lowercase internally. Accept both forms at the gateway.
    entry = _CATALOG.get(requested) or _CATALOG.get(str(requested).lower())
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
    # 所有 veya1.x 别名都注入 pi 的 provider key 池 (env 没设的 bai/scnet/gmi-serving 等
    # 也能从 ~/.pi/agent/models.json 拿到 key)。get_api_key 优先 config > env,
    # _PI_PROVIDER_CONFIG 已过滤掉以 '!' 开头的 shell 扩展 key (如 opencode-go),
    # 不会覆盖 env 中的有效凭据。
    if _PI_PROVIDER_CONFIG:
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


@app.get("/v1/free-pool")
async def free_pool_status() -> JSONResponse:
    """Expose lifecycle state for operators without exposing provider keys."""
    status = _FREE_POOL_LIFECYCLE.status()
    status.update(_FREE_POOL_RUNTIME)
    return JSONResponse(status)


@app.post("/v1/free-pool/refresh")
async def refresh_free_pool() -> JSONResponse:
    """Run an immediate reconciliation; useful after an upstream quota reset."""
    try:
        await asyncio.wait_for(
            _refresh_free_pool_once(), timeout=_FREE_POOL_REFRESH_TIMEOUT_SECONDS
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=502,
        )
    return JSONResponse({"ok": True, **_FREE_POOL_LIFECYCLE.status(), **_FREE_POOL_RUNTIME})


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
