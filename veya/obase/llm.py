"""
veya/llm.py — canonical multi-provider LLM client (facade)

Consolidates the provider layer (previously split across ``server/providers.py``
and the stub ``llm_call``/``llm_stream`` in ``veya/compat.py``) into a single
canonical entry point supporting:

- Non-streaming chat completion  (OpenAI-format response dict)
- Streaming chat completion     (SSE parsed to OpenAI delta events)
- Tool calling                  (OpenAI-compatible + Anthropic Messages API)
- Cost estimation               (approximate USD per provider)
- Graceful stub fallback        (when no API key is configured)

Providers: ``dashscope`` (qwen-plus), ``anthropic`` (claude-*), ``openai`` (gpt-*).

Selection order: ``config["provider"]`` > ``VEYA_LLM_PROVIDER`` env > ``veya1.2``.
API keys are read from ``{PROVIDER}_API_KEY`` env vars (or ``config["providers"]``).

Structure (obase self-contained base layer, SPEC v3.0 §3.4):
this module is the **facade** — the concern-separated implementation lives in
package-private siblings and is re-exported here so the historical
``veya.llm.*`` import path and monkeypatch surface stay byte-identical:

- :mod:`veya.obase._llm_config`   — pricing/endpoint/env tables + (provider,
  model)/API-key resolution
- :mod:`veya.obase._llm_protocol` — pure OpenAI ⇄ Anthropic wire translation +
  endpoint canonicalization
- :mod:`veya.obase._llm_transport` — httpx provider calls (``provider_call`` /
  ``provider_stream``)

The facade keeps the request-orchestration entry points (``llm_call`` /
``llm_stream`` / ``_aliased_llm_call`` / ``llm_call_routed``) and the
container/proxy helpers, which are monkeypatched together in the test suite and
therefore must resolve within this module's namespace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Re-export the extracted layers so ``veya.llm.<name>`` (import path +
# monkeypatch surface) is unchanged after the god-module decomposition.
from veya.obase._llm_config import (  # noqa: E402, F401 — facade re-export after logger
    _API_KEY_ENV,
    _DEFAULT_MODELS,
    _DEFAULT_PROVIDER,
    _ENDPOINTS,
    _PRICING,
    _opencode_go_key_from_auth,
    calc_cost,
    get_api_key,
)
from veya.obase._llm_protocol import (  # noqa: E402, F401 — facade re-export after logger
    _core_tool_schemas,
    _is_local_or_private,
    _normalize_anthropic_response,
    _normalize_chat_endpoint,
    _parse_image_url,
    _strip_empty_tool_calls,
    _to_anthropic_content_blocks,
    prepare_messages_for_provider,
)
from veya.obase._llm_transport import (  # noqa: E402, F401 — facade re-export after logger
    _call_anthropic,
    _call_openai_compat,
    provider_call,
    provider_stream,
)


def _user_llm_config() -> dict[str, str]:
    """用户主脑默认配置兜底: ~/.veya/config.json 的 llm 段。

    宿主与容器 (veya-data volume) 均可能配置; 无文件/损坏 → 空 dict。

    Lives in the facade (not ``_llm_config``) because the test suite
    monkeypatches ``veya.llm._user_llm_config`` and ``get_provider_config`` must
    resolve the patched name within this module's namespace.
    """
    try:
        p = Path.home() / ".veya" / "config.json"
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        llm = data.get("llm") or {}
        return {
            "provider": str(llm.get("provider") or "").lower(),
            "model": str(llm.get("model") or ""),
        }
    except Exception:
        return {}


def get_provider_config(
    config: dict | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[str, str]:
    """Resolve (provider, model) from explicit args → config → env → user config.json → defaults."""
    user_config = _user_llm_config()
    p = provider or (config or {}).get("provider")
    if not p:
        p = os.environ.get("VEYA_LLM_PROVIDER") or user_config.get("provider") or _DEFAULT_PROVIDER
    p = str(p).lower()
    m = model or (config or {}).get("model") or os.environ.get("VEYA_LLM_MODEL")
    if not m:
        # 用户主脑默认兜底 (config.json llm 段) — 否则无参调用落 anthropic/dashscope stub
        m = user_config.get("model") or _DEFAULT_MODELS.get(p, "default")
    return p, str(m)


def _in_container() -> bool:
    """容器环境检测 (与 engine_runner 一致)。"""
    return bool(os.environ.get("VEYA_WORKSPACE")) or os.path.exists("/.dockerenv")


def _custom_proxy_url(provider: str) -> str | None:
    """自定义 provider (非内置) 在容器内的代理兜底 URL。

    内置 provider (dashscope/openai/... 国内/官方直连) 返回 None;
    容器内经桥 17890 可达宿主代理 (7890, clash) 时返回代理 URL —
    海外自定义端点被 GFW 间歇重置 (could not reach) 时自动兜底。
    """
    if provider in _ENDPOINTS or not _in_container():
        return None
    import urllib.request

    for gw in ("192.168.16.1", "172.18.0.1"):
        try:
            with urllib.request.urlopen(f"http://{gw}:17890/", timeout=0.5) as resp:
                if resp.status == 200:
                    return f"http://{gw}:17890"
        except Exception:
            continue
    return None


def _ensure_frontier_bridge(endpoint: str, *, timeout_s: float = 8.0) -> bool:
    """确保本地 frontier 兜底桥 (opencodex, gpt-5.6-luna) 活着 — 探测 → 无则 spawn。

    frontier 兜底是「绝不静默」的最后一道防线, 但此前只在 server/engine_runner.py
    探测 codex 执行引擎可用性时被顺带 spawn (且仅容器内; 宿主机假设已由外部常驻
    进程管理, 从不探测/拉起)。若该副作用从未触发 (未选过 codex 引擎), 桥进程不
    存在, 兜底请求 connect-refused 后被调用处 `except Exception: pass` 静默吞掉,
    导致 opencode-go 网关一抖动, 错误就直接漏给用户 (与选哪个候选模型无关)。
    obase 是自洽底层, 不反向依赖 server.engine_runner — 这里自带一份幂等
    探测/拉起, 与 engine_runner._ensure_container_opencodex 逻辑对齐但独立运行。
    """
    import urllib.parse
    import urllib.request

    parsed = urllib.parse.urlparse(endpoint)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 10100
    if host not in ("127.0.0.1", "localhost"):
        return True  # 自定义 VEYA_FRONTIER_ENDPOINT (非本地) 假定外部管理, 不干预

    def _healthz() -> bool:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=0.5) as r:
                return bool(r.status == 200)
        except Exception:
            return False

    if _healthz():
        return True
    bun = (
        "/home/soffy/.nvm/versions/node/v26.4.0/lib/node_modules/"
        "@bitkyc08/opencodex/node_modules/bun/bin/bun.exe"
    )
    cli = (
        "/home/soffy/.nvm/versions/node/v26.4.0/lib/node_modules/"
        "@bitkyc08/opencodex/src/cli/index.ts"
    )
    if not (os.path.isfile(bun) and os.path.isfile(cli)):
        return False
    import subprocess
    import time

    env = dict(os.environ)
    if _in_container():
        gw = _container_gateway_ip_for_proxy()
        env.update(
            {
                "HTTPS_PROXY": f"http://{gw}:17890",
                "HTTP_PROXY": f"http://{gw}:17890",
                "NO_PROXY": "localhost,127.0.0.1",
                "no_proxy": "localhost,127.0.0.1",
            }
        )
    try:
        subprocess.Popen(
            [bun, cli, "start", "--port", str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        logger.warning("frontier 桥 (opencodex) spawn 失败: %s", exc)
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _healthz():
            return True
        time.sleep(0.4)
    return False


def _container_gateway_ip_for_proxy() -> str:
    """容器 → 宿主网关 IP (探测可达网段), 无可达网段兜底默认值。"""
    import urllib.error
    import urllib.request

    for gw in ("192.168.16.1", "172.18.0.1", "172.17.0.1"):
        try:
            with urllib.request.urlopen(f"http://{gw}:10101/v1/models", timeout=0.5) as resp:
                if resp.status in (200, 401, 403):
                    return gw
        except urllib.error.HTTPError as exc:
            if exc.code in (200, 401, 403):
                return gw
        except Exception:
            continue
    return "192.168.16.1"


# ---------------------------------------------------------------------------
# Framework-level entry points (used by compat, commands, TUI, routes)
# ---------------------------------------------------------------------------

_STUB_CONTENT = "LLM provider not configured — this is a shim response."

# ---------------------------------------------------------------------------
# Veya 1.2 主脑代理: GMI 默认 + OpenRouter 故障轮询 (round-robin)
# ---------------------------------------------------------------------------
# 首选 GMI MiniMax M3；GMI 失败时再轮询两个 OpenRouter 免费模型。
# 凭据分别由 GMI_API_KEY / OPENROUTER_API_KEY 注入；不回退到旧的
# OpenCode-Go 主脑池，也不保留过期模型名或 VEYA_ZEN_FREE_POOL 覆盖项。
_VEYA12_DEFAULT_POOL: list[dict[str, str]] = [
    {
        "provider": "bai",
        "model": "deepseek-v4-flash",
        "endpoint": "https://api.b.ai/v1",
    },
    {
        "provider": "gmi-serving",
        "model": "MiniMaxAI/MiniMax-M3",
        "endpoint": "https://api.gmi-serving.com/v1",
    },
    {
        "provider": "scnet",
        "model": "DeepSeek-V4-Flash",
        "endpoint": "https://api.scnet.cn/api/llm/v1",
    },
]

# veya1.2-free: opencode-go 免费模型轮询 (不走 veya1.2 主脑代理)。
# 端点统一指向本机 veya gateway (pid 8791) , opencode-go 走 chat/completions 协议。
# key 由 scripts/veya_llm_gateway.py 从 ~/.pi/agent/opencode-keys.txt 轮询注入。
# 5 个候选均经探活验证可用 (laguna-s-2.1-free 偶发 503, 轮询跳过即可) 。
_VEYA12_FREE_POOL: list[dict[str, str]] = [
    {
        "provider": "openai",
        "model": "opencode-go/hy3-free",
        "endpoint": "http://127.0.0.1:8791/v1/chat/completions",
    },
    {
        "provider": "openai",
        "model": "opencode-go/nemotron-3.5-lightning-free",
        "endpoint": "http://127.0.0.1:8791/v1/chat/completions",
    },
    {
        "provider": "openai",
        "model": "opencode-go/nemotron-3-ultra-free",
        "endpoint": "http://127.0.0.1:8791/v1/chat/completions",
    },
    {
        "provider": "openai",
        "model": "opencode-go/laguna-s-2.1-free",
        "endpoint": "http://127.0.0.1:8791/v1/chat/completions",
    },
    {
        "provider": "openai",
        "model": "opencode-go/ling-3.0-flash-fin-free",
        "endpoint": "http://127.0.0.1:8791/v1/chat/completions",
    },
    {
        "provider": "gmi-serving",
        "model": "MiniMaxAI/MiniMax-M3",
        "endpoint": "https://api.gmi-serving.com/v1",
    },
    {
        "provider": "bai",
        "model": "deepseek-v4-flash",
        "endpoint": "https://api.b.ai/v1",
    },
    {
        "provider": "bai",
        "model": "glm-5.3-flash",
        "endpoint": "https://api.b.ai/v1",
    },
    {
        "provider": "bai",
        "model": "mimo-v2.5",
        "endpoint": "https://api.b.ai/v1",
    },
    {
        "provider": "bai",
        "model": "hy3",
        "endpoint": "https://api.b.ai/v1",
    },
    {
        "provider": "bai",
        "model": "qwen3.8-flash",
        "endpoint": "https://api.b.ai/v1",
    },
    {
        "provider": "bai",
        "model": "deepseek-v4-flash-vision-exp",
        "endpoint": "https://api.b.ai/v1",
    },
]

# AIHubMix/Inferera public model catalog snapshot (2026-08-25).  The image-only
# gpt-image-2-free entry is intentionally excluded: veya1.2-free uses the
# chat/completions contract, while image generation has a separate API.
_INFERERA_FREE_MODELS: tuple[str, ...] = (
    "coding-glm-4.6-free",
    "coding-glm-4.7-free",
    "coding-glm-5-free",
    "coding-glm-5-turbo-free",
    "coding-glm-5.1-free",
    "coding-glm-5.2-free",
    "coding-kimi-k3-free",
    "coding-minimax-m2-free",
    "coding-minimax-m2.1-free",
    "coding-minimax-m2.5-free",
    "coding-minimax-m2.7-free",
    "coding-minimax-m3-free",
    "dots-3-note-preview-free",
    "gemini-3-flash-preview-free",
    "gemini-3.5-flash-lite-free",
    "gemini-3.6-flash-free",
    "gemini-3.7-flash-free",
    "gemma-4-26b-a4b-it-free",
    "gemma-4-31b-it-free",
    "glm-4.7-flash-free",
    "gpt-4.1-free",
    "gpt-4.1-mini-free",
    "gpt-4.1-nano-free",
    "gpt-4o-free",
    "gpt-5.5-free",
    "gpt-oss-20b-free",
    "k2.6-code-preview-free",
    "kimi-for-coding-free",
    "laguna-s-2.1-free",
    "laguna-xs-2.1-free",
    "lfm-2.5-2.6b-free",
    "ling-3.0-flash-free",
    "ling-3.0-tiny-free",
    "mimo-v2-flash-free",
    "nemotron-3-nano-30b-a3b-free",
    "nemotron-3-nano-omni-30b-a3b-reasoning-free",
    "nemotron-3-super-120b-a12b-free",
    "nemotron-3-ultra-550b-a55b-free",
    "nemotron-3.5-content-safety-free",
    "nemotron-3.5-lightning-free",
    "nemotron-nano-12b-v2-vl-free",
    "nemotron-nano-9b-v2-free",
    "north-mini-code-free",
    "qwen3.6-plus-preview-free",
    "xiaomi-mimo-v2-omni-free",
    "xiaomi-mimo-v2-pro-free",
    "xiaomi-mimo-v2.5-free",
    "xiaomi-mimo-v2.5-pro-free",
)
# Inferera catalog entries with context strictly below 512K.  These are moved
# to veya1.2-128K; dots-3-note-preview-free is exactly 512K and stays here.
_INFERERA_128K_MODELS: tuple[str, ...] = (
    "coding-glm-4.6-free",
    "coding-minimax-m2-free",
    "coding-minimax-m2.1-free",
    "coding-minimax-m2.5-free",
    "coding-minimax-m2.7-free",
    "coding-minimax-m3-free",
    "gemma-4-26b-a4b-it-free",
    "gemma-4-31b-it-free",
    "gpt-oss-20b-free",
    "k2.6-code-preview-free",
    "kimi-for-coding-free",
    "laguna-s-2.1-free",
    "laguna-xs-2.1-free",
    "lfm-2.5-2.6b-free",
    "ling-3.0-flash-free",
    "ling-3.0-tiny-free",
    "mimo-v2-flash-free",
    "nemotron-3-nano-30b-a3b-free",
    "nemotron-3-nano-omni-30b-a3b-reasoning-free",
    "nemotron-3-super-120b-a12b-free",
    "nemotron-3.5-content-safety-free",
    "nemotron-nano-12b-v2-vl-free",
    "nemotron-nano-9b-v2-free",
    "north-mini-code-free",
    "xiaomi-mimo-v2-omni-free",
    "xiaomi-mimo-v2-pro-free",
    "xiaomi-mimo-v2.5-free",
    "xiaomi-mimo-v2.5-pro-free",
)
_INFERERA_128K_MODEL_SET = frozenset(_INFERERA_128K_MODELS)
_VEYA12_FREE_POOL.extend(
    {"provider": "inferera", "model": model}
    for model in _INFERERA_FREE_MODELS
    if model not in _INFERERA_128K_MODEL_SET
)

# 进程内轮询游标 (asyncio 单线程, 普通 int 自增即可) — 跨调用推进以摊额度。
_zen_rr_cursor = 0
_veya12_free_rr_cursor = 0


def _veya12_pool() -> list[dict[str, str]]:
    """Veya 1.2 主脑池: GMI MiniMax M3 优先，OpenRouter 免费模型兜底。"""
    return list(_VEYA12_DEFAULT_POOL)


async def _frontier_fallback(messages: list[dict], kwargs: dict, *, reason: str) -> dict | None:
    """本地 frontier (gpt-5.6-luna) 兜底: 免费模型池全空/全失败时的最后一道防线。

    与主脑代理同款的短退避重试 + tool_calls 合法判断，供 Veya 1.2 复用。
    返回有效 resp (含 router 标记) 或 None (兜底也失败, 由调用方给结构化错误)。
    """
    _attempts = 4
    last_err = ""
    for attempt in range(_attempts):
        try:
            frontier_endpoint = kwargs.get("endpoint") or os.environ.get(
                "VEYA_FRONTIER_ENDPOINT", "http://127.0.0.1:10100/v1"
            )
            if not _ensure_frontier_bridge(frontier_endpoint):
                last_err = f"frontier 桥 (opencodex) 不可用: {frontier_endpoint}"
                raise RuntimeError(last_err)
            resp = await llm_call(
                messages,
                config=kwargs.get("config"),
                provider="openai",
                model="gpt-5.6-luna",
                endpoint=frontier_endpoint,
                tools=_core_tool_schemas(kwargs.get("tools")),
                default_content="gpt-5.6-luna 兜底失败",
            )
            fmsg = (resp.get("choices") or [{}])[0].get("message") or {}
            content = fmsg.get("content") or ""
            # tool_calls 场景 content 空是合法的 — 不可当无效内容拒绝。
            if fmsg.get("tool_calls") or (
                content.strip() and content.strip().lower() not in ("none", "null")
            ):
                resp["router"] = {"route": "frontier_fallback", "reason": reason}
                return resp
            last_err = f"gpt-5.6-luna 兜底返回无效内容: {content!r}"
        except Exception as exc:  # 兜底失败也绝不静默
            last_err = f"gpt-5.6-luna 兜底失败: {exc}"
        if attempt < _attempts - 1:
            logger.warning(
                "frontier 兜底第 %d 次失败 (%s), %.1fs 后重试",
                attempt + 1,
                last_err,
                2.0 * (2**attempt),
            )
            await asyncio.sleep(2.0 * (2**attempt))
    return None


async def _veya12_rr_call(
    messages: list[dict],
    kwargs: dict,
    *,
    pool: list[dict[str, str]],
    start: int,
    alias: str,
    route: str,
    pool_label: str,
    fallback_reason: str,
) -> dict:
    """Run one of the veya1.2 round-robin pools with common retry semantics."""
    candidates = pool[start:] + pool[:start]  # 从游标处旋转

    default = kwargs.get("default_content") or f"{pool_label}调用失败"
    last_err = ""
    _retry_rounds = 3
    for round_idx in range(_retry_rounds):
        for cand in candidates:
            cand_provider = cand["provider"]
            cand_model = cand["model"]
            call_kwargs: dict[str, Any] = {
                "config": kwargs.get("config"),
                "provider": cand_provider,
                "model": cand_model,
                "tools": kwargs.get("tools"),
                "default_content": default,
            }
            if cand.get("endpoint"):
                call_kwargs["endpoint"] = cand["endpoint"]
            try:
                resp = await llm_call(messages, **call_kwargs)
            except Exception as exc:  # 网络/鉴权失败 (含 openrouter 缺 key) → 换模型重试
                last_err = f"{cand_provider}/{cand_model}: {exc}"
                continue
            msg = (resp.get("choices") or [{}])[0].get("message") or {}
            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []
            # tool_calls 场景 content 空是合法的; stub/字面 None 视为无效。
            if (
                (not content.strip() and not tool_calls)
                or content.strip().lower() in ("none", "null")
                or (content.strip() and content.strip() == default and not tool_calls)
            ):
                last_err = f"{cand_provider}/{cand_model} 返回无效内容: {content!r}"
                continue
            resp.setdefault("usage", {})
            resp["router"] = {
                "route": route,
                "alias": alias,
                "provider": cand_provider,
                "model": cand_model,
            }
            return resp
        if round_idx < _retry_rounds - 1:
            logger.warning(
                "%s %s第 %d 轮全候选无效 (%s), %.1fs 后重试整轮",
                alias,
                pool_label,
                round_idx + 1,
                last_err,
                3.0 * (2**round_idx),
            )
            await asyncio.sleep(3.0 * (2**round_idx))

    fb = await _frontier_fallback(messages, kwargs, reason=fallback_reason)
    if fb is not None:
        return fb
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": (
                        f"{alias} {pool_label}调用失败: {last_err or '所有免费模型均失败'}"
                    ),
                }
            }
        ],
        "usage": {},
        "opencode": True,
        "error": True,
    }


async def _veya12_flash_call(messages: list[dict], kwargs: dict) -> dict:
    """veya1.2: GMI MiniMax M3 优先，OpenRouter 免费模型兜底。"""
    global _zen_rr_cursor
    pool = _veya12_pool()
    start = _zen_rr_cursor % len(pool)
    _zen_rr_cursor = (_zen_rr_cursor + 1) % len(pool)
    return await _veya12_rr_call(
        messages,
        kwargs,
        pool=pool,
        start=start,
        alias="veya1.2",
        route="gmi-openrouter-rr",
        pool_label="免费池",
        fallback_reason="veya1.2 GMI/OpenRouter pool empty → gpt-5.6-luna",
    )


async def _veya12_free_call(messages: list[dict], kwargs: dict) -> dict:
    """veya1.2-free: Inferera/AIHubMix 免费模型轮询。"""
    global _veya12_free_rr_cursor
    pool = list(_VEYA12_FREE_POOL)
    start = _veya12_free_rr_cursor % len(pool)
    _veya12_free_rr_cursor = (_veya12_free_rr_cursor + 1) % len(pool)
    return await _veya12_rr_call(
        messages,
        kwargs,
        pool=pool,
        start=start,
        alias="veya1.2-free",
        route="veya12-free-rr",
        pool_label="Inferera 免费池",
        fallback_reason="veya1.2-free pool empty → gpt-5.6-luna",
    )


# ---------------------------------------------------------------------------
# veya1.2-vl 别名: openrouter 免费图像/视频理解模型轮询 (round-robin)
# ---------------------------------------------------------------------------
# OpenRouter free 层 (:free 后缀, pricing=0) 里 architecture.input_modalities
# 含 video 的 4 个模型 (探活/核对于 2026-08-17, 用途是「看图/看视频回答」的
# 理解模型 — 不是文生视频/图生视频)。与 veya1.2-flash 同款 round-robin 设计,
# 走已内置的 openrouter provider (endpoint/pricing/API_KEY_ENV=OPENROUTER_API_KEY
# 见 _llm_config.py, 未改动)。OpenRouter free 限流按模型计费 (非账号总量):
# 20 req/min + 50~1000 req/day (视账号累计充值是否 ≥$10), 轮询摊到 4 个模型
# 聚合吞吐更高、更不容易撞单模型的分钟窗。
_OPENROUTER_VL_DEFAULT_POOL: list[str] = [
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
]

_openrouter_vl_rr_cursor = 0


def _openrouter_vl_pool() -> list[str]:
    """veya1.2-vl 免费视觉模型池: env VEYA_OPENROUTER_VL_POOL 覆盖, 否则默认。"""
    raw = os.environ.get("VEYA_OPENROUTER_VL_POOL", "").strip()
    if raw:
        pool = [m.strip() for m in raw.split(",") if m.strip()]
        if pool:
            return pool
    return list(_OPENROUTER_VL_DEFAULT_POOL)


def _has_visual_content(messages: list[dict]) -> bool:
    """消息里是否带图像/视频内容块 (image_url/image/video_url/video/input_video)。

    决定空池兜底能不能滑到本地 frontier (gpt-5.6-luna, 纯文本模型) — 若请求
    真带图/视频, frontier 看不见附件, 兜底会「盲答」且看似成功, 比明确报错
    更危险 (绝不静默 ≠ 绝不能报错, 而是不能悄悄给错答案)。
    """
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in (
                    "image_url",
                    "image",
                    "video_url",
                    "video",
                    "input_video",
                ):
                    return True
    return False


async def _veya12_vl_call(messages: list[dict], kwargs: dict) -> dict:
    """veya1.2-vl 别名: openrouter 免费图像/视频理解模型轮询直连 (+ 有条件 frontier 兜底)。

    与 _veya12_flash_call 同款轮询/重试策略, 池换成 OpenRouter 免费图像+视频
    理解模型。兜底例外: 请求真带图/视频时不滑向纯文本 frontier (会盲答),
    直接给结构化错误; 纯文本请求 (无附件) 才允许 frontier 兜底。
    """
    global _openrouter_vl_rr_cursor
    pool = _openrouter_vl_pool()
    start = _openrouter_vl_rr_cursor % len(pool)
    _openrouter_vl_rr_cursor = (_openrouter_vl_rr_cursor + 1) % len(pool)
    candidates = pool[start:] + pool[:start]  # 从游标处旋转

    default = kwargs.get("default_content") or "openrouter 调用失败"
    last_err = ""
    _retry_rounds = 3
    for round_idx in range(_retry_rounds):
        for cand in candidates:
            try:
                resp = await llm_call(
                    messages,
                    config=kwargs.get("config"),
                    provider="openrouter",
                    model=cand,
                    tools=kwargs.get("tools"),
                    default_content=default,
                )
            except Exception as exc:  # 网络/鉴权失败 → 换模型重试
                last_err = str(exc)
                continue
            msg = (resp.get("choices") or [{}])[0].get("message") or {}
            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []
            if (
                (not content.strip() and not tool_calls)
                or content.strip().lower() in ("none", "null")
                or (content.strip() and content.strip() == default and not tool_calls)
            ):
                last_err = f"openrouter {cand} 返回无效内容: {content!r}"
                continue
            resp.setdefault("usage", {})
            resp["router"] = {"route": "openrouter-vl-rr", "alias": "veya1.2-vl", "model": cand}
            return resp
        if round_idx < _retry_rounds - 1:
            logger.warning(
                "veya1.2-vl 免费池第 %d 轮全候选无效 (%s), %.1fs 后重试整轮",
                round_idx + 1,
                last_err,
                3.0 * (2**round_idx),
            )
            await asyncio.sleep(3.0 * (2**round_idx))

    if not _has_visual_content(messages):
        fb = await _frontier_fallback(
            messages, kwargs, reason="openrouter vl pool empty → gpt-5.6-luna"
        )
        if fb is not None:
            return fb
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": (f"openrouter 调用失败: {last_err or '所有免费视觉模型均失败'}"),
                }
            }
        ],
        "usage": {},
        "opencode": False,
        "error": True,
    }


# ---------------------------------------------------------------------------
# veya1.2-128K 别名: 小上下文免费模型轮询 (round-robin)
# ---------------------------------------------------------------------------
# openrouter 免费池 (:free, pricing=0) 里, 512K+ 大上下文的 3 个已进
# veya1.2-flash、图像/视频理解的 4 个已进 veya1.2-vl — 这里收 openrouter
# 免费池里剩下的通用文本模型 (2026-08-17 逐一探活确认真出内容; 排除 2 个:
# poolside/laguna-s-2.1:free 返回纯空白 padding 无有效 JSON, 探活当时判定
# 故障; nvidia/nemotron-3.5-content-safety:free 是安全审核分类器, 不响应
# 常规指令只吐"User Safety: safe"这类判定, 不是通用对话模型)。
_OPENROUTER_128K_DEFAULT_POOL: list[dict[str, str]] = [
    {"provider": "openrouter", "model": "cohere/north-mini-code:free"},
    {"provider": "openrouter", "model": "nvidia/nemotron-3-nano-30b-a3b:free"},
    {"provider": "openrouter", "model": "nvidia/nemotron-3-super-120b-a12b:free"},
    {"provider": "openrouter", "model": "poolside/laguna-xs-2.1:free"},
    {"provider": "openrouter", "model": "openrouter/free"},
    {"provider": "openrouter", "model": "liquid/lfm-2.5-2.6b:free"},
    {"provider": "openrouter", "model": "nvidia/nemotron-nano-9b-v2:free"},
    {"provider": "openrouter", "model": "openai/gpt-oss-20b:free"},
    {"provider": "openrouter", "model": "z-ai/glm-5.2:free"},
]
_OPENROUTER_128K_DEFAULT_POOL.extend(
    {"provider": "inferera", "model": model} for model in _INFERERA_128K_MODELS
)

_openrouter_128k_rr_cursor = 0


def _openrouter_128k_pool() -> list[dict[str, str]]:
    """veya1.2-128K 免费模型池: env 覆盖 OpenRouter 子池, 否则返回结构化候选。"""
    raw = os.environ.get("VEYA_OPENROUTER_128K_POOL", "").strip()
    if raw:
        pool = [{"provider": "openrouter", "model": m.strip()} for m in raw.split(",") if m.strip()]
        if pool:
            return pool
    return list(_OPENROUTER_128K_DEFAULT_POOL)


async def _veya12_128k_call(messages: list[dict], kwargs: dict) -> dict:
    """veya1.2-128K: OpenRouter 小上下文池 + Inferera 小模型轮询。"""
    global _openrouter_128k_rr_cursor
    pool = _openrouter_128k_pool()
    start = _openrouter_128k_rr_cursor % len(pool)
    _openrouter_128k_rr_cursor = (_openrouter_128k_rr_cursor + 1) % len(pool)
    return await _veya12_rr_call(
        messages,
        kwargs,
        pool=pool,
        start=start,
        alias="veya1.2-128K",
        route="veya12-128k-rr",
        pool_label="小上下文免费池",
        fallback_reason="veya1.2-128K pool empty → gpt-5.6-luna",
    )


async def _aliased_llm_call(messages: list[dict], kwargs: dict) -> dict:
    """兼容旧的 veya1.1 名称，统一转到 Veya 1.2 OpenRouter 代理。"""
    return await _veya12_flash_call(messages, kwargs)


async def llm_call(messages: list[dict], **kwargs: Any) -> dict:
    """Non-streaming chat completion.

    Resolves provider/model from ``kwargs`` (``config``/``provider``/``model``),
    ``VEYA_LLM_PROVIDER``/``VEYA_LLM_MODEL`` env, or defaults. Falls back to
    a stub response when no API key is configured (keeps offline tests green).
    """
    provider, model = get_provider_config(
        kwargs.get("config"), provider=kwargs.get("provider"), model=kwargs.get("model")
    )
    # veya1.1 兼容别名 → veya1.2 OpenRouter 主脑池。
    if model in ("veya1.1", "veya-1.1") or provider == "veya1.1":
        return await _aliased_llm_call(messages, kwargs)
    # 长的子别名 (含后缀 -free/-vl/-128K) 必须在 veya1.2 之前判定,
    # 避免被 user_config["provider"]="veya1.2" 填充后误中主脑代理。
    if model in ("veya1.2-free", "veya-1.2-free") or provider == "veya1.2-free":
        return await _veya12_free_call(messages, kwargs)
    if model in ("veya1.2-vl", "veya-1.2-vl") or provider == "veya1.2-vl":
        return await _veya12_vl_call(messages, kwargs)
    if model in ("veya1.2-128K", "veya1.2-128k", "veya-1.2-128K") or provider in (
        "veya1.2-128K",
        "veya1.2-128k",
    ):
        return await _veya12_128k_call(messages, kwargs)
    # veya1.2 主脑代理: OpenRouter 免费模型轮询
    if model in ("veya1.2-flash", "veya-1.2-flash") or model == "veya1.2" or provider in (
        "veya1.2-flash",
        "veya1.2",
    ):
        return await _veya12_flash_call(messages, kwargs)
    config = kwargs.get("config") or {}
    # 自定义 endpoint: 顶层 kwarg > config["endpoints"][provider] > config["base_url"](NVIDIA NIM 等)
    endpoint = (
        kwargs.get("endpoint")
        or (config.get("endpoints") or {}).get(provider)
        or config.get("base_url")
        or os.environ.get("VEYA_LLM_ENDPOINT")
        or _ENDPOINTS.get(provider)
    )
    # 归一化到完整 chat/completions URL (base URL 形态自动补全) —
    # 提前到本作用域: 错误信息/重试看到的是真实请求 URL
    if endpoint:
        try:
            endpoint = _normalize_chat_endpoint(endpoint, provider)
        except ValueError as exc:
            content = kwargs.get("default_content", f"{_STUB_CONTENT} ({exc})")
            return {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
    # 本地/内网模型 (Ollama / opencodex / 网关桥) 无需 API Key
    local_endpoint = _is_local_or_private(endpoint)
    if not get_api_key(provider, kwargs.get("config")) and not local_endpoint:
        content = kwargs.get("default_content", _STUB_CONTENT)
        return {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    timeout = kwargs.get("timeout", 120.0)
    tools = kwargs.get("tools")
    max_tokens = kwargs.get("max_tokens", 4096)
    temperature = kwargs.get("temperature")
    tool_choice = kwargs.get("tool_choice")
    # 专属 Key 注入: config["providers"][provider] 优先于环境变量(Genesis 物理隔离)
    api_key = get_api_key(provider, config)
    retries = int(kwargs.get("retries", 2))

    # 双通道客户端: 直连 + 代理兜底 (自定义海外端点被 GFW 间歇重置时)
    # 内置 provider (dashscope 等国内直连) 不走代理; 容器内经桥 17890 → 宿主 7890。
    proxy = _custom_proxy_url(provider)
    clients: list[httpx.AsyncClient] = [httpx.AsyncClient(timeout=timeout)]
    if proxy:
        clients.append(httpx.AsyncClient(timeout=timeout, proxy=proxy))
    try:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            client = clients[attempt % len(clients)]
            try:
                return await provider_call(
                    client,
                    provider,
                    model=model,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    endpoint=endpoint,
                    api_key=api_key,
                    tool_choice=tool_choice,
                )
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
                httpx.ReadError,
            ) as exc:
                # 瞬时网络抖动(如 NIM 连接重置) — 指数退避重试 (直连/代理双通道交替)
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(1.5 * (2**attempt))
        raise last_exc if last_exc else RuntimeError("llm_call retry exhausted")
    except ValueError as exc:
        # Missing key etc. — degrade to stub rather than crashing the caller.
        content = kwargs.get("default_content", f"{_STUB_CONTENT} ({exc})")
        return {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    except httpx.HTTPStatusError as exc:
        # Provider rejected the request (bad key, rate limit, unknown model, ...) —
        # surface the status + body instead of a raw 500 with no explanation.
        status = exc.response.status_code
        detail = exc.response.text.strip()[:300]
        content = kwargs.get(
            "default_content",
            f"{provider} rejected the request (HTTP {status}): {detail or 'no detail returned'}",
        )
        return {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    except httpx.HTTPError as exc:
        # Network/timeout/connect errors talking to the provider endpoint.
        content = kwargs.get(
            "default_content",
            f"could not reach {provider} ({endpoint or 'default endpoint'}): {exc}",
        )
        return {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


async def llm_stream(messages: list[dict], **kwargs: Any) -> AsyncIterator[dict]:
    """Streaming chat completion (OpenAI delta events), stub fallback."""
    provider, model = get_provider_config(
        kwargs.get("config"), provider=kwargs.get("provider"), model=kwargs.get("model")
    )
    config = kwargs.get("config") or {}
    endpoint = (
        kwargs.get("endpoint")
        or (config.get("endpoints") or {}).get(provider)
        or config.get("base_url")
        or os.environ.get("VEYA_LLM_ENDPOINT")
        or _ENDPOINTS.get(provider)
    )
    if not get_api_key(provider, config) and not _is_local_or_private(endpoint):
        content = kwargs.get("default_content", "LLM streaming not configured — shim.")
        for word in content.split():
            yield {"choices": [{"delta": {"content": word + " "}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        return

    timeout = kwargs.get("timeout", 120.0)
    tools = kwargs.get("tools")
    max_tokens = kwargs.get("max_tokens", 4096)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async for event in provider_stream(
                client,
                provider,
                model=model,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
                endpoint=endpoint,
            ):
                yield event
        except (ValueError, httpx.HTTPError) as exc:
            # 本地兜底 endpoint 免 key 后会真请求 (见上短路条件): 服务没起/离线时
            # provider_stream 抛 HTTPStatusError/连接错误 —— 与 llm_call 的兜底对齐,
            # 优雅降级 stub 而非崩。无 key 时用 "not configured" 措辞 (等同短路语义)。
            if not get_api_key(provider, config):
                content = kwargs.get("default_content", "LLM streaming not configured — shim.")
            else:
                content = f"{_STUB_CONTENT} ({exc})"
            for word in content.split():
                yield {"choices": [{"delta": {"content": word + " "}}]}
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


# ---------------------------------------------------------------------------
# 路由调用 (freellmapi 机制内化: 统一模型 fallover / 用量跟踪 / 粘性 / 工具救援)
# ---------------------------------------------------------------------------

from veya.obase.model_routing import (  # noqa: E402
    StickySession,
    UsageLedger,
    get_route,
    rescue_tool_calls,
)

# 逻辑模型 → 各 provider 的实际模型名 (未列出则同名)
_PROVIDER_MODEL_ALIAS: dict[tuple[str, str], str] = {
    ("deepseek-chat", "openrouter"): "deepseek/deepseek-chat",
    ("gpt-4o-mini", "openrouter"): "openai/gpt-4o-mini",
}


def _provider_model(logical_model: str, provider: str) -> str:
    """逻辑模型 → provider 实际模型名。"""
    return _PROVIDER_MODEL_ALIAS.get((logical_model, provider), logical_model)


async def llm_call_routed(
    messages: list[dict],
    *,
    logical_model: str | None = None,
    session_id: str | None = None,
    config: dict | None = None,
    ledger: UsageLedger | None = None,
    sticky: StickySession | None = None,
    max_attempts: int = 3,
    **kwargs: Any,
) -> dict:
    """路由版 llm_call: 统一模型 → provider 组 fallover + 用量跟踪 + 粘性 + 工具救援。

    Args:
        messages: 对话消息。
        logical_model: 逻辑模型名; None 用 kwargs["model"] 或默认 provider 模型。
            注册过路由 (register_route) 则走组内 fallover, 否则单 provider 直调。
        session_id: 粘性会话 id; 提供后同会话 TTL 内锁定逻辑模型。
        config / ledger / sticky: 可注入共享实例 (默认新建)。
        max_attempts: 组内最大尝试次数 (每 provider 一次)。
        **kwargs: 透传 llm_call (tools/max_tokens/temperature...)。

    Returns:
        OpenAI 格式响应; 若模型输出文本 tool call 则自动救援为结构化
        tool_calls (附带 ``_rescue: true`` 标记)。
    """
    ledger = ledger or UsageLedger()
    sticky = sticky or StickySession()

    # 粘性锁定: 已锁则用锁定模型
    if session_id:
        locked = sticky.get(session_id)
        if locked:
            logical_model = locked
    if logical_model is None:
        _, logical_model = get_provider_config(config, model=kwargs.get("model"))
    if session_id:
        sticky.lock(session_id, logical_model)

    providers = get_route(logical_model) or [get_provider_config(config, model=logical_model)[0]]
    attempts: list[dict[str, Any]] = []
    last_error = "no provider succeeded"

    for provider in providers[:max_attempts]:
        model = _provider_model(logical_model, provider)
        # 用量门禁: 已超限的 provider 跳过
        ok, view = ledger.check(provider, model)
        if not ok:
            attempts.append(
                {
                    "provider": provider,
                    "model": model,
                    "error": "quota exceeded",
                    "over": view["over"],
                }
            )
            continue
        try:
            response = await llm_call(
                messages,
                config=config,
                provider=provider,
                model=model,
                **{k: v for k, v in kwargs.items() if k not in ("config", "provider", "model")},
            )
        except Exception as exc:  # 网络/超时/provider 异常 → 学习限额 + 下一位
            ledger.learn_limit(provider, model, error_body=str(exc))
            attempts.append(
                {"provider": provider, "model": model, "error": f"{exc.__class__.__name__}: {exc}"}
            )
            last_error = str(exc)
            continue

        # 用量记录 (成功才算)
        usage = response.get("usage") or {}
        ledger.record(
            provider,
            model,
            prompt_tokens=usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0,
            completion_tokens=usage.get("completion_tokens", 0)
            or usage.get("output_tokens", 0)
            or 0,
        )
        response["_routed"] = {"provider": provider, "model": model, "attempts": attempts}
        # 工具调用救援: 文本 tool call → 结构化
        content = (response.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if isinstance(content, str) and not response.get("choices", [{}])[0].get("message", {}).get(
            "tool_calls"
        ):
            rescued = rescue_tool_calls(content)
            if rescued:
                response["choices"][0]["message"]["tool_calls"] = rescued
                response["_rescue"] = True
        return response

    # 全组失败: 返回结构化错误 (保留尝试轨迹)
    return {
        "_error": True,
        "error": last_error,
        "attempts": attempts,
        "logical_model": logical_model,
        "providers": providers,
    }
