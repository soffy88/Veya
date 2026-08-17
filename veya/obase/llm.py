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

Selection order: ``config["provider"]`` > ``VEYA_LLM_PROVIDER`` env > ``dashscope``.
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
from veya.obase._llm_config import (  # noqa: F401
    _API_KEY_ENV,
    _DEFAULT_MODELS,
    _DEFAULT_PROVIDER,
    _ENDPOINTS,
    _PRICING,
    _opencode_go_key_from_auth,
    calc_cost,
    get_api_key,
)
from veya.obase._llm_protocol import (  # noqa: F401
    _core_tool_schemas,
    _is_local_or_private,
    _normalize_anthropic_response,
    _normalize_chat_endpoint,
    _parse_image_url,
    _strip_empty_tool_calls,
    _to_anthropic_content_blocks,
    prepare_messages_for_provider,
)
from veya.obase._llm_transport import (  # noqa: F401
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
    p = provider or (config or {}).get("provider")
    if not p:
        p = os.environ.get("VEYA_LLM_PROVIDER", _DEFAULT_PROVIDER)
    p = str(p).lower()
    m = model or (config or {}).get("model") or os.environ.get("VEYA_LLM_MODEL")
    if not m:
        # 用户主脑默认兜底 (config.json llm 段) — 否则无参调用落 anthropic/dashscope stub
        m = _user_llm_config().get("model") or _DEFAULT_MODELS.get(p, "default")
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
                return r.status == 200
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
# veya1.2-flash 别名: opencode zen 免费模型轮询 (round-robin)
# ---------------------------------------------------------------------------
# veya1.1 是顺序候选 (永远先 deepseek-v4-flash) — 单模型很快吃光当日额度。
# veya1.2-flash 改用轮询: 每次调用从池里下一个模型起转, 把负载/额度摊到多个
# 免费模型上, 单模型耗尽/抖动时自然滑到下一个。
# 池成员取「显式标 -free/每日免费额度」的模型 (opencode.ai zen 网页口径,
# 2026-08-17 用户核对): nemotron-3.5-lightning-free / nemotron-3-ultra-free /
# laguna-s-2.1-free / hy3-free / mimo-v2.5-free / big-pickle /
# deepseek-v4-flash-free — 均在通用 /zen/v1 端点下 (不是 /zen/go/v1 的 go 池,
# 那边同名模型无 -free 后缀且按量计费; 探活: /zen/go/v1/models 里查不到这些
# -free ID, 只有 /zen/v1/models 有)。原池 (deepseek-v4-flash/qwen3.7-plus/
# kimi-k2.7-code, /zen/go/v1) 按量计费不是免费额度, 已整体替换。
# nemotron 系推理链较长, 默认 max_tokens=4096 (_llm_transport.py) 够用;
# 探活当时 mimo-v2.5-free/big-pickle/deepseek-v4-flash-free 命中
# FreeUsageLimitError (今日额度已被同一 key 的其它探活耗尽) — 报错类型本身
# 印证这几个确是被追踪配额的免费模型, 非模型失效, 保留入池。
# 池跨两个 provider (opencode zen 免费 + openrouter 免费), 每项显式带
# provider/model/endpoint (endpoint 缺省时落 _ENDPOINTS[provider] 默认值,
# 见 _llm_transport.py 的 `endpoint or _ENDPOINTS.get(provider, ...)`)。
# 2026-08-17 用户提供 OPENROUTER_API_KEY 后, 从 openrouter 免费模型里选了
# 3 个上下文 ≥512K 的补进池: nemotron-3-ultra-550b-a55b(1M) /
# nemotron-3.5-lightning(1M, openrouter 侧, 与 opencode zen 侧同名模型是
# 不同 provider 的独立候选) / dots-3-note-preview(512K) — 均逐一探活过
# (curl 直连 openrouter.ai, 拿到真实 content, 非猜测)。
# 用户可用 VEYA_ZEN_FREE_POOL (逗号分隔裸模型 ID, 均按 opencode-go/zen/v1
# 解析 — 与默认池的 openrouter 成员是分开的, 覆盖只替换 opencode-go 那一段
# 不动 openrouter 3 个) 覆盖/扩展 opencode-go 侧子池。
_ZEN_FREE_DEFAULT_POOL: list[dict[str, str]] = [
    {
        "provider": "opencode-go",
        "model": "nemotron-3.5-lightning-free",
        "endpoint": "https://opencode.ai/zen/v1",
    },
    {
        "provider": "opencode-go",
        "model": "nemotron-3-ultra-free",
        "endpoint": "https://opencode.ai/zen/v1",
    },
    {
        "provider": "opencode-go",
        "model": "laguna-s-2.1-free",
        "endpoint": "https://opencode.ai/zen/v1",
    },
    {"provider": "opencode-go", "model": "hy3-free", "endpoint": "https://opencode.ai/zen/v1"},
    {
        "provider": "opencode-go",
        "model": "mimo-v2.5-free",
        "endpoint": "https://opencode.ai/zen/v1",
    },
    {"provider": "opencode-go", "model": "big-pickle", "endpoint": "https://opencode.ai/zen/v1"},
    {
        "provider": "opencode-go",
        "model": "deepseek-v4-flash-free",
        "endpoint": "https://opencode.ai/zen/v1",
    },
    {"provider": "openrouter", "model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
    {"provider": "openrouter", "model": "nvidia/nemotron-3.5-lightning:free"},
    {"provider": "openrouter", "model": "dots-studio/dots-3-note-preview:free"},
]

# 进程内轮询游标 (asyncio 单线程, 普通 int 自增即可) — 跨调用推进以摊额度。
_zen_rr_cursor = 0


def _zen_free_pool() -> list[dict[str, str]]:
    """veya1.2-flash 免费模型池: env VEYA_ZEN_FREE_POOL 覆盖 opencode-go 子池,
    否则默认 (opencode zen 7 个 + openrouter 3 个大上下文免费模型)。"""
    raw = os.environ.get("VEYA_ZEN_FREE_POOL", "").strip()
    if raw:
        opencode_go = [
            {
                "provider": "opencode-go",
                "model": m.strip(),
                "endpoint": "https://opencode.ai/zen/v1",
            }
            for m in raw.split(",")
            if m.strip()
        ]
        if opencode_go:
            openrouter = [e for e in _ZEN_FREE_DEFAULT_POOL if e["provider"] == "openrouter"]
            return opencode_go + openrouter
    return list(_ZEN_FREE_DEFAULT_POOL)


async def _frontier_fallback(messages: list[dict], kwargs: dict, *, reason: str) -> dict | None:
    """本地 frontier (gpt-5.6-luna) 兜底: 免费模型池全空/全失败时的最后一道防线。

    与 _aliased_llm_call 内联兜底同款逻辑 (短退避重试 + tool_calls 合法判断),
    独立成函数供 veya1.2-flash 复用 — 不改动 veya1.1 久经验证的兜底路径。
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
        except Exception as exc:  # noqa: BLE001 — 兜底失败也绝不静默
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


async def _veya12_flash_call(messages: list[dict], kwargs: dict) -> dict:
    """veya1.2-flash 别名: opencode zen 免费模型轮询直连 (+ 本地 frontier 兜底)。

    每次调用从免费池下一个模型起转 (round-robin, 摊额度), 逐个候选试;
    候选返回空/字面 None/等于 stub 即滑下一个; 全候选一轮全无效 → 短退避后
    整轮重来 (覆盖网关瞬时抖动); 全轮次仍失败 → gpt-5.6-luna 兜底; 兜底也
    失败 → 结构化错误 (error 标记跳过质量闸门, 绝不静默)。
    """
    global _zen_rr_cursor
    pool = _zen_free_pool()
    start = _zen_rr_cursor % len(pool)
    _zen_rr_cursor = (_zen_rr_cursor + 1) % len(pool)
    candidates = pool[start:] + pool[:start]  # 从游标处旋转

    default = kwargs.get("default_content") or "免费池调用失败"
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
                "route": "zen-free-rr",
                "alias": "veya1.2-flash",
                "provider": cand_provider,
                "model": cand_model,
            }
            return resp
        if round_idx < _retry_rounds - 1:
            logger.warning(
                "veya1.2-flash 免费池第 %d 轮全候选无效 (%s), %.1fs 后重试整轮",
                round_idx + 1,
                last_err,
                3.0 * (2**round_idx),
            )
            await asyncio.sleep(3.0 * (2**round_idx))

    fb = await _frontier_fallback(messages, kwargs, reason="zen free pool empty → gpt-5.6-luna")
    if fb is not None:
        return fb
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": (
                        f"veya1.2-flash 免费池调用失败: {last_err or '所有免费模型均失败'}"
                    ),
                }
            }
        ],
        "usage": {},
        "opencode": True,
        "error": True,
    }


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
# veya1.2-128K 别名: openrouter 剩余免费文本模型轮询 (round-robin)
# ---------------------------------------------------------------------------
# openrouter 免费池 (:free, pricing=0) 里, 512K+ 大上下文的 3 个已进
# veya1.2-flash、图像/视频理解的 4 个已进 veya1.2-vl — 这里收 openrouter
# 免费池里剩下的通用文本模型 (2026-08-17 逐一探活确认真出内容; 排除 2 个:
# poolside/laguna-s-2.1:free 返回纯空白 padding 无有效 JSON, 探活当时判定
# 故障; nvidia/nemotron-3.5-content-safety:free 是安全审核分类器, 不响应
# 常规指令只吐"User Safety: safe"这类判定, 不是通用对话模型)。
_OPENROUTER_128K_DEFAULT_POOL: list[str] = [
    "cohere/north-mini-code:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "poolside/laguna-xs-2.1:free",
    "openrouter/free",
    "liquid/lfm-2.5-2.6b:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "openai/gpt-oss-20b:free",
    "z-ai/glm-5.2:free",
]

_openrouter_128k_rr_cursor = 0


def _openrouter_128k_pool() -> list[str]:
    """veya1.2-128K 免费模型池: env VEYA_OPENROUTER_128K_POOL 覆盖, 否则默认。"""
    raw = os.environ.get("VEYA_OPENROUTER_128K_POOL", "").strip()
    if raw:
        pool = [m.strip() for m in raw.split(",") if m.strip()]
        if pool:
            return pool
    return list(_OPENROUTER_128K_DEFAULT_POOL)


async def _veya12_128k_call(messages: list[dict], kwargs: dict) -> dict:
    """veya1.2-128K 别名: openrouter 剩余免费文本模型轮询直连 (+ 本地 frontier 兜底)。

    与 _veya12_vl_call 同款轮询/重试/兜底策略, 池换成 openrouter 免费池里
    的通用文本模型 (非视觉, 无需 vl 那种"带图跳过 frontier"的特判)。
    """
    global _openrouter_128k_rr_cursor
    pool = _openrouter_128k_pool()
    start = _openrouter_128k_rr_cursor % len(pool)
    _openrouter_128k_rr_cursor = (_openrouter_128k_rr_cursor + 1) % len(pool)
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
            resp["router"] = {"route": "openrouter-128k-rr", "alias": "veya1.2-128K", "model": cand}
            return resp
        if round_idx < _retry_rounds - 1:
            logger.warning(
                "veya1.2-128K 免费池第 %d 轮全候选无效 (%s), %.1fs 后重试整轮",
                round_idx + 1,
                last_err,
                3.0 * (2**round_idx),
            )
            await asyncio.sleep(3.0 * (2**round_idx))

    fb = await _frontier_fallback(
        messages, kwargs, reason="openrouter 128K pool empty → gpt-5.6-luna"
    )
    if fb is not None:
        return fb
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": (f"openrouter 调用失败: {last_err or '所有免费模型均失败'}"),
                }
            }
        ],
        "usage": {},
        "opencode": False,
        "error": True,
    }


async def _aliased_llm_call(messages: list[dict], kwargs: dict) -> dict:
    """veya1.1 别名路由: 决策 → 单发 (short/text/tool/code/reason/vision) 或
    并行分派 (long) → 归一为 llm_call 返回格式。"""
    from veya.platform import load as _load_oskill

    oskill = _load_oskill("oskill")
    router = oskill.LLMRouter()

    async def _single(payload: dict) -> dict:
        if payload["provider"] == "opencode":
            # opencode-go 网关 key 直连 (OpenAI 兼容端点 /zen/go/v1) — 非 CLI
            # agent 模式: 主脑 system prompt 完整生效, 模型不裹 opencode 人格。
            # 矩阵 model 形如 opencode-go/deepseek-v4-flash → API 只认裸 ID。
            model = str(payload.get("model") or "opencode-go/deepseek-v4-flash")
            _, _, bare = model.partition("/")
            bare = bare or model
            # 网关对部分输入会返回字面量 'None'/空 (抖动) → 换模型重试,
            # 全失败 → 结构化错误消息 (error 标记跳过质量闸门), 绝不静默。
            # 另: default_content 是失败时的 stub (如 'opencode-go 调用失败'),
            # 必须与真实回答区分 — 否则错误消息被当有效内容返回且跳过兜底。
            # 用户指示 (2026-08-16): 不用 mimo 系列（工具调用场景不稳定）。
            # 备选换 kimi-k2.7-code（代码/agent 模型, 网关实测可用）。
            alt_models = ["opencode-go/kimi-k2.7-code", "opencode-go/deepseek-v4-flash"]
            candidates = [model] + [m for m in alt_models if m != model]
            last_err = ""
            default = kwargs.get("default_content") or "opencode-go 调用失败"
            # 网关抖动通常是瞬时的 (几秒内自愈) — 候选模型轮完一遍仍全部
            # 空/无效时, 不直接判死刑: 等一下再整轮重来, 给网关喘息时间。
            # 轮次间指数退避, 轮数收敛 (绝不无限重试); 用户不该在这里被
            # 硬报错卡住 (用户反馈 2026-08-16: 抖动应等待重试, 不能报错即停)。
            # 用户反馈 (2026-08-16 第二次): 实测网关+frontier 会同时抖动 70s+
            # (原 20s 总预算扛不住) — 轮数/退避拉长到共约 90s, 覆盖分钟级抖动。
            _retry_rounds = 5
            for round_idx in range(_retry_rounds):
                for cand in candidates:
                    _, _, cand_bare = cand.partition("/")
                    cand_bare = cand_bare or cand
                    try:
                        resp = await llm_call(
                            payload["messages"],
                            config=kwargs.get("config"),
                            provider="opencode-go",
                            model=cand_bare,
                            endpoint="https://opencode.ai/zen/go/v1",
                            tools=payload.get("tools"),
                            default_content=default,
                        )
                    except Exception as exc:  # 网络/鉴权失败 → 换模型重试
                        last_err = str(exc)
                        continue
                    content = ((resp.get("choices") or [{}])[0].get("message") or {}).get(
                        "content"
                    ) or ""
                    tool_calls = ((resp.get("choices") or [{}])[0].get("message") or {}).get(
                        "tool_calls"
                    ) or []
                    # 有 tool_calls 的响应 content 为空是合法的 (opencode 模型把
                    # 输出放 reasoning_content + tool_calls) — 不可误判为无效。
                    # stub 检测: 返回 default_content 说明网关失败, 继续下一候选。
                    if (
                        (not content.strip() and not tool_calls)
                        or content.strip().lower() in ("none", "null")
                        or (content.strip() and content.strip() == default and not tool_calls)
                    ):
                        last_err = f"opencode-go {cand_bare} 返回无效内容: {content!r}"
                        continue
                    return resp
                if round_idx < _retry_rounds - 1:
                    logger.warning(
                        "opencode-go 全候选模型第 %d 轮均无效 (%s), %.1fs 后重试整轮",
                        round_idx + 1,
                        last_err,
                        3.0 * (2**round_idx),
                    )
                    await asyncio.sleep(3.0 * (2**round_idx))
            # 全部候选/全部轮次空/失败 → 本地 frontier (gpt-5.6-luna) 兜底: free 池
            # 本地模型零网络抖动, 保证「绝不静默」在模型层彻底闭环。容器内走
            # 宿主桥 192.168.16.1; 宿主默认 127.0.0.1:10100。
            # frontier 桥本身偶尔也会抖 (容器→宿主桥接路径), 同样不能一次
            # 失败就判死刑 — 短退避重试几次 (本地零网络延迟, 退避比网关候选短)。
            _frontier_attempts = 4
            for f_attempt in range(_frontier_attempts):
                try:
                    frontier_endpoint = kwargs.get("endpoint") or os.environ.get(
                        "VEYA_FRONTIER_ENDPOINT", "http://127.0.0.1:10100/v1"
                    )
                    if not _ensure_frontier_bridge(frontier_endpoint):
                        last_err = f"frontier 桥 (opencodex) 不可用: {frontier_endpoint}"
                        raise RuntimeError(last_err)
                    resp = await llm_call(
                        payload["messages"],
                        config=kwargs.get("config"),
                        provider="openai",
                        model="gpt-5.6-luna",
                        endpoint=frontier_endpoint,
                        # 本地模型上下文有限: 兜底时裁剪为核心工具面 (保回复优先)
                        tools=_core_tool_schemas(payload.get("tools")),
                        default_content="gpt-5.6-luna 兜底失败",
                    )
                    fmsg = (resp.get("choices") or [{}])[0].get("message") or {}
                    content = fmsg.get("content") or ""
                    ftool_calls = fmsg.get("tool_calls") or []
                    # tool_calls 场景 content 为空是合法的 (模型把输出放 tool_calls) —
                    # 与 opencode-go 候选循环同款判断, 不可当无效内容拒绝, 否则
                    # 带工具且该调工具的请求会被确定性误杀 (重试也救不了)。
                    if ftool_calls or (
                        content.strip() and content.strip().lower() not in ("none", "null")
                    ):
                        resp["router"] = {
                            "route": "frontier_fallback",
                            "reason": "opencode-go empty → gpt-5.6-luna",
                        }
                        return resp
                    # 无异常但内容仍空/等于 stub — 必须覆盖 last_err, 否则报错会
                    # 误导性地停留在上一个 opencode-go 候选模型上 (掩盖 frontier 才是真因)。
                    last_err = f"gpt-5.6-luna 兜底返回无效内容: {content!r}"
                except Exception as exc:  # noqa: BLE001 — 兜底失败也绝不静默
                    last_err = f"gpt-5.6-luna 兜底失败: {exc}"
                if f_attempt < _frontier_attempts - 1:
                    logger.warning(
                        "frontier 兜底第 %d 次失败 (%s), %.1fs 后重试",
                        f_attempt + 1,
                        last_err,
                        2.0 * (2**f_attempt),
                    )
                    await asyncio.sleep(2.0 * (2**f_attempt))
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (f"opencode-go 调用失败: {last_err or '所有模型均失败'}"),
                        }
                    }
                ],
                "usage": {},
                "opencode": True,
                "error": True,
            }
        return await llm_call(
            payload["messages"],
            config=kwargs.get("config"),
            provider=payload["provider"],
            model=payload["model"],
            tools=payload.get("tools"),
            endpoint=payload.get("endpoint") or kwargs.get("endpoint"),
            default_content=kwargs.get("default_content"),
        )

    # 用户要求 (2026-08): 大模型直接用 opencode-go API — 不走 oskill
    # 复杂别名路由器 (quality-gate 升级/模型切换/并行分派把链路搞复杂,
    # 是空回复的诱因)。直连: 候选模型重试 + 空回复 gpt-5.6-luna 兜底
    # (逻辑在 _single 的 opencode 分支)。
    result = await _single(
        {
            "provider": "opencode",
            "model": "opencode-go/deepseek-v4-flash",
            "messages": messages,
            "tools": kwargs.get("tools"),
        }
    )
    # opencode-go 档: provider_call 不认识 opencode → 已由 _single 特判返回
    if result.get("opencode"):
        return result
    result.setdefault("usage", {})
    result["router"] = {"route": "opencode-direct", "alias": "veya1.1"}
    # 外环兜底 (绝不静默): 无论内部哪个路径 (opencode 空 / quality gate 升级
    # 到全量工具超限等) 漏出空回复, 最后用本地 gpt-5.6-luna + 核心工具面
    # 兜一次。兜底是可靠性, 不是路由判断 — 模型决策不受影响。
    msg = (result.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content") or ""
    if (not content.strip() or content.strip().lower() in ("none", "null")) and not msg.get(
        "tool_calls"
    ):
        try:
            frontier_endpoint = kwargs.get("endpoint") or os.environ.get(
                "VEYA_FRONTIER_ENDPOINT", "http://127.0.0.1:10100/v1"
            )
            if not _ensure_frontier_bridge(frontier_endpoint):
                raise RuntimeError(f"frontier 桥 (opencodex) 不可用: {frontier_endpoint}")
            resp = await llm_call(
                messages,
                config=kwargs.get("config"),
                provider="openai",
                model="gpt-5.6-luna",
                endpoint=frontier_endpoint,
                tools=_core_tool_schemas(kwargs.get("tools")),
                default_content="gpt-5.6-luna 兜底失败",
            )
            m2 = (resp.get("choices") or [{}])[0].get("message") or {}
            c2 = m2.get("content") or ""
            # tool_calls 场景 content 空是合法的 — 与候选循环同款判断, 不误杀。
            if (m2.get("tool_calls")) or (
                c2.strip() and c2.strip().lower() not in ("none", "null")
            ):
                resp["router"] = {"route": "frontier_fallback", "reason": "empty → gpt-5.6-luna"}
                return resp
        except Exception:  # noqa: BLE001 — 兜底失败仍返回原结果 (后续各层继续兜底)
            pass
    return result


async def llm_call(messages: list[dict], **kwargs: Any) -> dict:
    """Non-streaming chat completion.

    Resolves provider/model from ``kwargs`` (``config``/``provider``/``model``),
    ``VEYA_LLM_PROVIDER``/``VEYA_LLM_MODEL`` env, or defaults. Falls back to
    a stub response when no API key is configured (keeps offline tests green).
    """
    provider, model = get_provider_config(
        kwargs.get("config"), provider=kwargs.get("provider"), model=kwargs.get("model")
    )
    # veya1.1 智能路由别名: 按任务类型路由 (deepseek-v4-flash / qwen3.7-flash),
    # 长输入并行分派快速回答 (RouteLLM 3O 内化, 见 docs/prd/LLM_ROUTER_PRD.md)
    if model in ("veya1.1", "veya-1.1") or provider == "veya1.1":
        return await _aliased_llm_call(messages, kwargs)
    # veya1.2-flash 别名: opencode zen 免费模型轮询 (round-robin 摊每日额度)
    if model in ("veya1.2-flash", "veya-1.2-flash", "veya1.2") or provider in (
        "veya1.2-flash",
        "veya1.2",
    ):
        return await _veya12_flash_call(messages, kwargs)
    # veya1.2-vl 别名: openrouter 免费图像/视频理解模型轮询 (round-robin)
    if model in ("veya1.2-vl", "veya-1.2-vl") or provider == "veya1.2-vl":
        return await _veya12_vl_call(messages, kwargs)
    # veya1.2-128K 别名: openrouter 剩余免费文本模型轮询 (round-robin)
    if model in ("veya1.2-128K", "veya1.2-128k", "veya-1.2-128K") or provider in (
        "veya1.2-128K",
        "veya1.2-128k",
    ):
        return await _veya12_128k_call(messages, kwargs)
    config = kwargs.get("config") or {}
    # 自定义 endpoint: 顶层 kwarg > config["endpoints"][provider] > config["base_url"](NVIDIA NIM 等)
    endpoint = (
        kwargs.get("endpoint")
        or (config.get("endpoints") or {}).get(provider)
        or config.get("base_url")
        or os.environ.get("VEYA_LLM_ENDPOINT")
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
    if not get_api_key(provider, kwargs.get("config")):
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
            ):
                yield event
        except ValueError as exc:
            yield {"choices": [{"delta": {"content": f"{_STUB_CONTENT} ({exc}) "}}]}
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
