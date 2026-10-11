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
import re
import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from veya.obase import llm as _llm
from veya.obase._llm_config import get_api_key
from veya.obase._llm_transport import _opencode_session_headers
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
# 入站鉴权: 本机 / docker 网桥免鉴权 (magpie、relay、容器照旧), 其余来源
# (tailnet、局域网) 必须带 VEYA_GATEWAY_KEYS 中的任一 Bearer key。
# ---------------------------------------------------------------------------
import ipaddress as _ipaddress

from fastapi.responses import JSONResponse as _AuthJSONResponse

_GATEWAY_KEYS = {k.strip() for k in os.environ.get("VEYA_GATEWAY_KEYS", "").split(",") if k.strip()}
_TRUSTED_NETS = [
    _ipaddress.ip_network(n)
    for n in os.environ.get("VEYA_GATEWAY_TRUSTED_NETS", "127.0.0.0/8,::1/128,172.16.0.0/12").split(",")
    if n.strip()
]


@app.middleware("http")
async def _inbound_auth(request: Request, call_next):
    host = request.client.host if request.client else ""
    try:
        addr = _ipaddress.ip_address(host)
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        trusted = any(addr in net for net in _TRUSTED_NETS)
    except ValueError:
        trusted = False
    if not trusted:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not _GATEWAY_KEYS or token not in _GATEWAY_KEYS:
            return _AuthJSONResponse(
                {"error": {"message": "unauthorized", "type": "auth_error"}}, status_code=401
            )
    return await call_next(request)

# ---------------------------------------------------------------------------
# 模型目录: 静态别名/frontier + 启动时从 opencode zen 拉取的全量直连模型。
# veya1.2-free 的活动池由下方生命周期任务独立维护。
# ---------------------------------------------------------------------------
_STATIC_CATALOG: dict[str, dict[str, Any]] = {
    # --- SPEC v1.0 §2: the four canonical proxies come first ---
    "veya1.2": {"model": "veya1.2"},
    "veya-free": {"model": "veya-free"},
    "veya-nim": {"model": "veya-nim"},
    "veya-vl": {"model": "veya-vl"},
    # --- deprecated spellings, kept resolvable per §11 ---
    "veya1.1": {"model": "veya1.1"},
    "veya1.2-flash": {"model": "veya1.2-flash"},
    "veya1.2-free": {"model": "veya1.2-free"},
    "veya1.2-vl": {"model": "veya1.2-vl"},
    # The dedicated 128K long-context pool is retired, but the alias must stay
    # admitted: §11 requires a deprecated spelling to resolve — to veya-free
    # now — with the substitution stamped, not to be rejected outright. Dropping
    # it here would turn a visible deprecation into an opaque 400 for every
    # caller that still has the old id in its config.
    "veya1.2-128K": {"model": "veya1.2-128K"},
    "veya-dp4.1-jev-1.13": {"model": "veya-dp4.1-jev-1.13"},
    # workbuddy 多模型轮询池: 单模型频繁 429, 轮询 + 按 retry_after 冷却 + 失败自动换下一个。
    "veya-wb": {"model": "veya-wb", "pool": "workbuddy"},
    "veya-fllm": {"model": "veya-fllm", "pool": "freellmapi"},
    # opencodex (10100) retired 2026-10-12; ChatGPT frontier now comes via CLIProxyAPI.
    "gpt-5.6-luna": {
        "provider": "cliproxy",
        "model": "gpt-5.6-luna",
        "endpoint": "http://127.0.0.1:8317/v1",
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


# ---------------------------------------------------------------------------
# 本机上游: 把已部署的本地路由 (CLIProxyAPI / FreeLLMAPI) 并入网关目录,
# 让 veya 网关成为唯一入口。key 从各自本地配置读取 (可被 *_API_KEY env 覆盖),
# 只写进本进程 env, 让 llm_call / 免费池探活的 get_api_key 统一解析。
# ---------------------------------------------------------------------------
_LOCAL_UPSTREAMS: dict[str, str] = {
    "cliproxy": os.environ.get("VEYA_CLIPROXY_BASE", "http://127.0.0.1:8317/v1"),
    "freellmapi": os.environ.get("VEYA_FREELLMAPI_BASE", "http://127.0.0.1:3001/v1"),
    "workbuddy": os.environ.get("VEYA_WORKBUDDY_BASE", "http://127.0.0.1:8788/v1"),
}
_NON_CHAT_MARKERS = ("image", "embed", "tts", "whisper", "diffusion", "codex-auto-review")


def _read_local_upstream_key(name: str) -> str:
    try:
        if name == "cliproxy":
            import yaml

            cfg = yaml.safe_load((Path.home() / "cliproxyapi" / "config.yaml").read_text())
            keys = (cfg or {}).get("api-keys") or []
            return str(keys[0]) if keys else ""
        if name == "workbuddy":
            pi_cfg = json.loads((Path.home() / ".pi" / "agent" / "models.json").read_text())
            return str(((pi_cfg.get("providers") or {}).get("workbuddy") or {}).get("apiKey") or "")
        if name == "freellmapi":
            import sqlite3

            db = Path.home() / "freellmapi" / "server" / "data" / "freeapi.db"
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "SELECT value FROM settings WHERE key = 'unified_api_key'"
                ).fetchone()
            finally:
                con.close()
            return str(row[0]) if row and row[0] else ""
    except Exception as exc:  # 本地上游缺失不阻塞网关
        print(f"[veya-llm-gateway] 警告: 读取 {name} key 失败: {type(exc).__name__}", file=sys.stderr)
    return ""


def _local_upstream_key(name: str) -> str:
    env_name = f"{name.upper()}_API_KEY"
    key = os.environ.get(env_name, "") or _read_local_upstream_key(name)
    if key:
        os.environ[env_name] = key
    return key


def _fetch_local_upstream_catalog() -> dict[str, dict[str, Any]]:
    """启动时把本机上游的聊天模型挂成 ``<upstream>/<model>`` 目录项。"""
    catalog: dict[str, dict[str, Any]] = {}
    for name, base in _LOCAL_UPSTREAMS.items():
        key = _local_upstream_key(name)
        if not key:
            print(f"[veya-llm-gateway] 警告: 无 {name} key, 跳过", file=sys.stderr)
            continue
        try:
            resp = httpx.get(
                f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=8.0
            )
            resp.raise_for_status()
            ids = [str(m.get("id") or "") for m in resp.json().get("data", [])]
        except Exception as exc:
            print(f"[veya-llm-gateway] 警告: 拉取 {name} 目录失败: {exc}", file=sys.stderr)
            continue
        for mid in ids:
            if not mid or any(marker in mid.lower() for marker in _NON_CHAT_MARKERS):
                continue
            catalog[f"{name}/{mid}"] = {"provider": name, "model": mid, "endpoint": base}
    return catalog


_CATALOG: dict[str, dict[str, Any]] = {
    **_STATIC_CATALOG,
    **_fetch_zen_catalog(),
    **_fetch_local_upstream_catalog(),
}
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
_FREE_POOL_REFRESH_TIMEOUT_SECONDS = _env_float("VEYA_FREE_POOL_REFRESH_TIMEOUT_SECONDS", 180.0)
_FREE_POOL_FAILURE_THRESHOLD = max(1, int(os.environ.get("VEYA_FREE_POOL_FAILURE_THRESHOLD", "3")))
_FREE_POOL_MAX_ACTIVE = max(0, int(os.environ.get("VEYA_FREE_POOL_MAX_ACTIVE", "32")))
_FREE_POOL_MAX_DISCOVERED_PER_SOURCE = max(
    1, int(os.environ.get("VEYA_FREE_POOL_MAX_DISCOVERED_PER_SOURCE", "24"))
)
_FREE_POOL_STATE_PATH = os.environ.get("VEYA_FREE_POOL_STATE", "").strip() or str(
    Path.home() / ".veya" / "free-pool-state.json"
)

# The lifecycle is kept for its in-process discovery + probe reconciliation, but
# persistence is off.  The veya-free pool's authority is ~/.veya/model-state.json
# (SPEC §5/§7); the persisted state file had drifted far behind the running
# gateway and was still being written, so a reader could believe 18 models were
# active when every one of them was unknown to the live pool.  Nothing is
# restored from disk at startup any more either — a stale record must never be
# able to seed the fallback pool.
_FREE_POOL_LIFECYCLE = FreePoolLifecycle(
    _llm._VEYA12_FREE_POOL,
    state_path=_FREE_POOL_STATE_PATH,
    failure_threshold=_FREE_POOL_FAILURE_THRESHOLD,
    max_active=_FREE_POOL_MAX_ACTIVE,
    persist=False,
)
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


def _free_pool_sources() -> list[tuple[str, str]]:
    """Discovery sources, filtered by ``VEYA_FREE_POOL_PROVIDERS``.

    Same knob as ``canonical_proxies._free_pool_allowed_providers`` so the
    discovered pool and the routing-side filter can never disagree: dropping a
    provider at discovery only, or only at routing, leaves a pool whose
    members the router would still try.
    """
    all_sources = [
        ("opencode-go", "https://opencode.ai/zen/v1"),
        ("opencode-go", "https://opencode.ai/zen/go/v1"),
        ("openrouter", "https://openrouter.ai/api/v1"),
        ("inferera", "https://api.inferera.com/v1"),
        ("bai", "https://api.b.ai/v1"),
        ("gmi-serving", "https://api.gmi-serving.com/v1"),
        ("freellmapi", _LOCAL_UPSTREAMS["freellmapi"]),
    ]
    raw = os.environ.get("VEYA_FREE_POOL_PROVIDERS", "").strip()
    if not raw:
        return all_sources
    allowed = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return [s for s in all_sources if s[0] in allowed]


async def _discover_free_pool() -> FreePoolSnapshot:
    """Discover free text candidates and complete source catalogs."""
    sources = _free_pool_sources()
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
            if source == "freellmapi":
                # FreeLLMAPI 自己在 ~300 个免费模型间做 failover; 只收它的 auto
                # 入口, 不把它的成员逐个灌进池子 (否则与 openrouter :free 重复探活)。
                if model_id != "auto":
                    continue
                entry = {
                    "provider": "freellmapi",
                    "model": model_id,
                    "endpoint": base,
                    "source": source,
                }
                key = entry_key(entry)
                if key not in entries:
                    entries[key] = entry
                    per_source_count[source] = per_source_count.get(source, 0) + 1
                continue
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
    if source == "opencode-go" and (
        not endpoint or (urlparse(endpoint).hostname or "") in {"127.0.0.1", "localhost", "::1"}
    ):
        # A seed may point at the local gateway.  Lifecycle probes must bypass
        # that gateway to avoid probing the service recursively; discovered
        # entries keep their own base (zen/v1 for free, zen/go/v1 for Go).
        endpoint = "https://opencode.ai/zen/v1"
    key = _provider_key(provider)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    # opencode zen rejects requests without x-opencode-session (400 MissingSessionID).
    headers.update(_opencode_session_headers(endpoint))
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
        message = (data.get("choices") or [{}])[0].get("message") or {}
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


_REJECTION_RE = re.compile(r"^\S+ rejected the request \(HTTP (\d{3})\)")


def _upstream_rejection(resp: dict[str, Any]) -> tuple[int, str] | None:
    """Detect llm_call's fallback 'X rejected the request (HTTP nnn)' reply."""
    msg = (resp.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content")
    if msg.get("tool_calls") or not isinstance(content, str):
        return None
    match = _REJECTION_RE.match(content.strip())
    if not match:
        return None
    status = int(match.group(1))
    if status < 400 or status > 599:
        status = 502
    return status, content.strip()[:2000]


# ---------------------------------------------------------------------------
# veya-wb: workbuddy 轮询池
# ---------------------------------------------------------------------------
_POOL_ENV = {"workbuddy": "VEYA_WB_POOL", "freellmapi": "VEYA_FLLM_POOL"}
_POOL_ALIAS = {"workbuddy": "veya-wb", "freellmapi": "veya-fllm"}
_POOL_COOLDOWN: dict[str, dict[str, float]] = {k: {} for k in _POOL_ENV}
_POOL_RR: dict[str, int] = {k: 0 for k in _POOL_ENV}
_POOL_LAST: dict[str, dict[str, Any]] = {k: {} for k in _POOL_ENV}
_POOL_TIMEOUT = {"workbuddy": 120.0, "freellmapi": float(os.environ.get("VEYA_FLLM_TIMEOUT", "30"))}
_POOL_MAX_TRIES = {"workbuddy": 0, "freellmapi": int(os.environ.get("VEYA_FLLM_MAX_TRIES", "6"))}
_POOL_DEADLINE = {"workbuddy": 0.0, "freellmapi": float(os.environ.get("VEYA_FLLM_DEADLINE", "75"))}


def _pool_models(upstream: str) -> list[str]:
    raw = os.environ.get(_POOL_ENV[upstream], "").strip()
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    if upstream == "workbuddy":
        return sorted(k.split("/", 1)[1] for k in _CATALOG if k.startswith("workbuddy/"))
    return []


def _wb_pool_models() -> list[str]:
    return _pool_models("workbuddy")


def _wb_cooldown_seconds(status: int, message: str) -> float:
    if status == 429:
        m = re.search(r"retry(?:_after\"?:?|\s+in|-after:?)\s*(\d+)", message, re.I)
        return float(m.group(1)) if m else 300.0
    if status in (401, 403):
        return 1800.0
    if status >= 500:
        return 120.0
    return 60.0


async def _pool_call(upstream: str, messages: list[Any], tools: Any) -> tuple[dict[str, Any] | None, str, tuple[int, str] | None]:
    alias = _POOL_ALIAS[upstream]
    pool = _pool_models(upstream)
    if not pool:
        return None, "", (503, f"{alias}: 池为空")
    cool = _POOL_COOLDOWN[upstream]
    base = _LOCAL_UPSTREAMS[upstream]
    now = time.time()
    start = _POOL_RR[upstream] % len(pool)
    _POOL_RR[upstream] = start + 1
    last_err: tuple[int, str] | None = None
    tries = 0
    for i in range(len(pool)):
        model = pool[(start + i) % len(pool)]
        if cool.get(model, 0) > now:
            continue
        if _POOL_MAX_TRIES[upstream] and tries >= _POOL_MAX_TRIES[upstream]:
            break
        if _POOL_DEADLINE[upstream] and time.time() - now > _POOL_DEADLINE[upstream]:
            break
        tries += 1
        kwargs: dict[str, Any] = {"model": model, "provider": upstream, "endpoint": base, "tools": tools}
        if _PI_PROVIDER_CONFIG:
            kwargs["config"] = _PI_PROVIDER_CONFIG
        try:
            resp = await asyncio.wait_for(llm_call(messages, **kwargs), timeout=_POOL_TIMEOUT[upstream])
        except Exception as exc:  # 单个模型异常/超时不拖垮整个池
            last_err = (502 if not isinstance(exc, asyncio.TimeoutError) else 504, f"{model}: {type(exc).__name__}: {exc}")
            cool[model] = time.time() + 120
            print(f"[{alias}] {model} -> {type(exc).__name__}, 冷却 120s", file=sys.stderr)
            continue
        err = _upstream_rejection(resp)
        if err is None:
            _POOL_LAST[upstream].update({"model": model, "at": time.time()})
            return resp, model, None
        status, message = err
        cool[model] = time.time() + _wb_cooldown_seconds(status, message)
        last_err = (status, f"{model}: {message}")
        print(f"[{alias}] {model} -> {status}, 冷却 {int(cool[model] - time.time())}s", file=sys.stderr)
    if last_err is None:
        soonest = min(cool.get(m, now) for m in pool)
        last_err = (429, f"{alias}: 池内 {len(pool)} 个模型全部冷却中, 最早 {int(soonest - now)}s 后恢复")
    return None, "", last_err


def _pool_status(upstream: str) -> dict[str, Any]:
    now = time.time()
    pool = _pool_models(upstream)
    cool = _POOL_COOLDOWN[upstream]
    return {
        "pool": pool,
        "cooling": {m: int(t - now) for m, t in cool.items() if t > now},
        "available": [m for m in pool if cool.get(m, 0) <= now],
        "last": _POOL_LAST[upstream],
    }


@app.get("/v1/wb-pool")
async def wb_pool_status() -> JSONResponse:
    return JSONResponse(_pool_status("workbuddy"))


@app.get("/v1/fllm-pool")
async def fllm_pool_status() -> JSONResponse:
    return JSONResponse(_pool_status("freellmapi"))


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
    wb_model = ""
    if entry.get("pool") in _POOL_ENV:
        pool_up = entry["pool"]
        resp, wb_model, wb_err = await _pool_call(pool_up, messages, tools)
        if resp is None:
            status, message = wb_err or (502, f"{_POOL_ALIAS[pool_up]} failed")
            return JSONResponse(
                {"error": {"message": message, "type": "upstream_error", "code": status}},
                status_code=status,
            )
        resp.setdefault("router", {"model": f"{pool_up}/{wb_model}"})
    else:
        resp = await llm_call(messages, **call_kwargs)
    upstream_error = _upstream_rejection(resp)
    if upstream_error is not None:
        status, message = upstream_error
        # llm_call 把上游 HTTP 报错包成一条 assistant 文本; 对外原样当 200 返回
        # 会让 magpie 等前置路由误判成功、不做 failover。这里还原成真实错误码。
        return JSONResponse(
            {"error": {"message": message, "type": "upstream_error", "code": status}},
            status_code=status,
        )
    resp_id = f"{requested}-{int(time.time() * 1000)}"
    created = int(time.time())
    # The public Veya contract exposes the requested logical alias as the
    # response identity.  Preserve the provider-selected upstream model only
    # in the controlled routing trace; clients must not mistake it for the
    # canonical Veya model.
    router = resp.get("router") or resp.get("_routed") or {}
    resolved_model = router.get("model") if isinstance(router, dict) else None
    if resolved_model and resolved_model != requested:
        resp["veya_routing"] = {
            "requested_model": requested,
            "resolved_model": resolved_model,
            "alias": requested,
        }
    if body.get("stream"):
        return StreamingResponse(
            _sse_from_resp(resp, resp_id, created, requested), media_type="text/event-stream"
        )
    resp.setdefault("id", resp_id)
    resp.setdefault("object", "chat.completion")
    resp.setdefault("created", created)
    resp["model"] = requested
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
