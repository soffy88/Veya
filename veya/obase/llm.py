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
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Re-export the extracted layers so ``veya.llm.<name>`` (import path +
# monkeypatch surface) is unchanged after the god-module decomposition.
from veya.obase import canonical_proxies as _cp  # noqa: E402 — SPEC v1.0 §2/§10 canonical proxies
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

# NVIDIA NIM aliases backed by the shared Stratum key pool.  The model ids are
# deliberately kept in one table so aliases remain stable when an upstream
# NIM model name is changed through an environment override.
_NVIDIA_NIM_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
_NVIDIA_NIM_ALIASES: dict[str, str] = {
    "veya-m3-nv": "minimaxai/minimax-m3",
    # NVIDIA deprecated the unversioned endpoint and now exposes the live
    # Flash deployment under the dated model id.
    "veya-deepseek-v4-flash-nv": "deepseek-ai/deepseek-v4-flash-0731",
    "veya-qwen3.5-397b-nv": "qwen/qwen3.5-397b-a17b",
    "veya-kimi-k2.6-nv": "moonshotai/kimi-k2.6",
    "veya-glm5.1-nv": "z-ai/glm5.1",
}
_NVIDIA_NIM_MODEL_ENV: dict[str, str] = {
    "veya-m3-nv": "VEYA_NIM_M3_MODEL",
    "veya-deepseek-v4-flash-nv": "VEYA_NIM_DEEPSEEK_MODEL",
    "veya-qwen3.5-397b-nv": "VEYA_NIM_QWEN_MODEL",
    "veya-kimi-k2.6-nv": "VEYA_NIM_KIMI_MODEL",
    "veya-glm5.1-nv": "VEYA_NIM_GLM_MODEL",
}
_nvidia_nim_cursors: dict[str, int] = {alias: 0 for alias in _NVIDIA_NIM_ALIASES}
_nvidia_nim_cursor_lock = threading.Lock()


def _nvidia_nim_keys() -> list[str]:
    """Load NIM keys without ever logging or exposing their values.

    ``NVIDIA_NIM_KEY_POOL``/``NIM_KEY_POOL`` are preferred for containers;
    otherwise read the Stratum pipeline key file.  The file is intentionally
    not committed and can be mounted read-only into the Veya container.
    """
    raw = os.environ.get("NVIDIA_NIM_KEY_POOL") or os.environ.get("NIM_KEY_POOL")
    if raw:
        values = raw.split(",")
    else:
        values = []
        configured = os.environ.get("VEYA_NVIDIA_NIM_KEYS_FILE", "")
        paths = [configured] if configured else []
        paths.extend(
            [
                "/data/soffy/projects/stratum/aii/.pipeline_keys.json",
                "/home/soffy/projects/stratum/aii/.pipeline_keys.json",
                str(
                    Path(__file__).resolve().parents[4] / "stratum" / "aii" / ".pipeline_keys.json"
                ),
            ]
        )
        for candidate in paths:
            if not candidate:
                continue
            try:
                data = json.loads(Path(candidate).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(data, dict):
                values = list(data.values())
                break
    keys: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = str(value).strip()
        if key and key not in seen:
            keys.append(key)
            seen.add(key)
    if not keys:
        single = os.environ.get("NVIDIA_NIM_API_KEY", "").strip()
        if single:
            keys.append(single)
    return keys


def _nvidia_nim_model(alias: str) -> str:
    return os.environ.get(_NVIDIA_NIM_MODEL_ENV[alias], "").strip() or _NVIDIA_NIM_ALIASES[alias]


def _next_nvidia_nim_index(alias: str, size: int) -> int:
    with _nvidia_nim_cursor_lock:
        index = _nvidia_nim_cursors[alias] % size
        _nvidia_nim_cursors[alias] = (index + 1) % size
        return index


async def _nvidia_nim_call(messages: list[dict], kwargs: dict, alias: str) -> dict:
    """Call one NIM model, rotating through the shared Stratum keys per request.

    DEPRECATED (SPEC v1.0 §21 P9): the five ``veya-*-nv`` aliases now resolve to
    the ``veya-nim`` canonical proxy, whose pool is built from qualified models
    in ``~/.veya/model-state.json``.  Kept reachable so the legacy path is not
    deleted mid-migration; every model id in :data:`_NVIDIA_NIM_ALIASES` is
    retired upstream (410 EOL / 404 removed / 404 no entitlement).  This runner
    also stays the source of the alias table that
    ``scripts/veya_llm_gateway.py`` imports for its static catalog.
    """
    keys = _nvidia_nim_keys()
    if not keys:
        return {
            "choices": [
                {"message": {"role": "assistant", "content": f"{alias} 未配置 NVIDIA NIM key"}}
            ],
            "usage": {},
            "error": True,
        }
    start = _next_nvidia_nim_index(alias, len(keys))
    timeout = kwargs.get("timeout", 120.0)
    attempts = min(len(keys), max(1, int(kwargs.get("retries", 2)) + 1))
    last_error = ""
    async with httpx.AsyncClient(timeout=timeout) as client:
        for offset in range(attempts):
            key = keys[(start + offset) % len(keys)]
            try:
                response = await provider_call(
                    client,
                    "openai",
                    model=_nvidia_nim_model(alias),
                    messages=messages,
                    tools=kwargs.get("tools"),
                    max_tokens=kwargs.get("max_tokens", 4096),
                    temperature=kwargs.get("temperature"),
                    endpoint=_NVIDIA_NIM_ENDPOINT,
                    api_key=key,
                    tool_choice=kwargs.get("tool_choice"),
                )
                response.setdefault("router", {})
                response["router"].update(
                    {
                        "route": "nvidia-nim-key-rr",
                        "alias": alias,
                        "model": _nvidia_nim_model(alias),
                    }
                )
                return response
            except (httpx.HTTPError, ValueError) as exc:
                last_error = str(exc)
    return {
        "choices": [
            {"message": {"role": "assistant", "content": f"{alias} 调用失败: {last_error}"}}
        ],
        "usage": {},
        "error": True,
    }


async def _nvidia_nim_stream(messages: list[dict], kwargs: dict, alias: str) -> AsyncIterator[dict]:
    """Streaming counterpart of :func:`_nvidia_nim_call`."""
    keys = _nvidia_nim_keys()
    if not keys:
        yield {"choices": [{"delta": {"content": f"{alias} 未配置 NVIDIA NIM key"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        return
    key = keys[_next_nvidia_nim_index(alias, len(keys))]
    async with httpx.AsyncClient(timeout=kwargs.get("timeout", 120.0)) as client:
        try:
            async for event in provider_stream(
                client,
                "openai",
                model=_nvidia_nim_model(alias),
                messages=messages,
                tools=kwargs.get("tools"),
                max_tokens=kwargs.get("max_tokens", 4096),
                endpoint=_NVIDIA_NIM_ENDPOINT,
                api_key=key,
            ):
                yield event
            return
        except (httpx.HTTPError, ValueError) as exc:
            content = f"{alias} 调用失败: {exc}"
    for word in content.split():
        yield {"choices": [{"delta": {"content": word + " "}}]}
    yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


# ---------------------------------------------------------------------------
# Veya 1.2 主脑代理:
# 首选 opencode-go / deepseek-v4.1-flash + OpenRouter 免费兜底。
# GMI MiniMax M3 已于 2026-09 移除: 上游持续 402 Insufficient balance。
# Advisor + Executor 架构保留为独立 alias veya-dp4.1-jev-1.13，
# 不改变固化的 veya1.2 单一主入口路由语义。
_VEYA12_DEFAULT_POOL: list[dict[str, str]] = [
    {
        "provider": "opencode-go",
        "model": "deepseek-v4.1-flash",
        "endpoint": "https://opencode.ai/zen/go/v1",
    },
    {
        "provider": "openrouter",
        "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
    },
    {"provider": "openrouter", "model": "minimax/minimax-m3:free"},
]

# veya1.2-free: opencode-go 免费模型轮询 (不走 veya1.2 主脑代理)。
# 端点统一指向本机 veya gateway，端口由 VEYA_GATEWAY_PORT 控制；opencode-go
# 走 chat/completions 协议。默认 8791 保持旧版常驻服务兼容。
# key 由 scripts/veya_llm_gateway.py 从 ~/.pi/agent/opencode-keys.txt 轮询注入。
# 2026-10-10 重新验证: 原 opencode-go free (403 FreeTierError) / gmi-serving
# (402) / bai (ConnectTimeout) 种子全部失效已移除; 现种子为 5 个实测可用的
# OpenRouter :free 模型。网关 lifecycle 每 24h 重新探测并替换活动池。
# 上游偶发慢响应由 _veya12_rr_call 的 12s 超时 + rejected-content 跳过处理。
_VEYA_GATEWAY_PORT = os.environ.get("VEYA_GATEWAY_PORT", "8791").strip() or "8791"
_VEYA_GATEWAY_CHAT_ENDPOINT = f"http://127.0.0.1:{_VEYA_GATEWAY_PORT}/v1/chat/completions"

_VEYA12_FREE_POOL: list[dict[str, str]] = [
    {
        "provider": "openrouter",
        "model": "liquid/lfm-2.5-2.6b:free",
        "endpoint": "https://openrouter.ai/api/v1",
        "source": "openrouter",
    },
    {
        "provider": "openrouter",
        "model": "cohere/north-mini-code:free",
        "endpoint": "https://openrouter.ai/api/v1",
        "source": "openrouter",
    },
    {
        "provider": "openrouter",
        "model": "poolside/laguna-s-2.1:free",
        "endpoint": "https://openrouter.ai/api/v1",
        "source": "openrouter",
    },
    {
        "provider": "openrouter",
        "model": "dots-studio/dots-3-note-preview:free",
        "endpoint": "https://openrouter.ai/api/v1",
        "source": "openrouter",
    },
    {
        "provider": "openrouter",
        "model": "nvidia/nemotron-3.5-lightning:free",
        "endpoint": "https://openrouter.ai/api/v1",
        "source": "openrouter",
    },
]

# Inferera 免费额度在 2026-08-30 探测时已耗尽，所有原 free-only 候选均从
# veya1.2-free 活动池删除；保留空快照名，防止后续代码误把旧列表重新注册。
# 原先注册在这里的 28 个 <512K 小上下文模型随 veya1.2-128K 长上下文池一起
# 退役：它们的唯一上游就是配额已耗尽的 Inferera，留着只会让 alias 解析到一个
# 必然失败的池。veya1.2-128K 现按 §11 直接落到 veya-free 资格化池。
_INFERERA_FREE_MODELS: tuple[str, ...] = ()

# 进程内轮询游标 (asyncio 单线程, 普通 int 自增即可) — 跨调用推进以摊额度。
# veya1.2 主脑池不轮转 (固定从首位 opencode-go 开始, 其余为有序兜底);
# 仅 veya1.2-free 免费池仍用游标摊额度。
_veya12_free_rr_cursor = 0


def _veya12_pool() -> list[dict[str, str]]:
    """Veya 1.2 主脑池: opencode-go DeepSeek V4.1 Flash 优先, 其余依次兜底。"""
    return list(_VEYA12_DEFAULT_POOL)


def _replace_veya12_free_pool(pool: list[dict[str, str]]) -> None:
    """Atomically replace the runtime free pool after lifecycle reconciliation."""
    global _VEYA12_FREE_POOL
    _VEYA12_FREE_POOL = [
        dict(entry) for entry in pool if entry.get("provider") and entry.get("model")
    ]


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
                # 单候选超时: 免费模型上游波动大, 卡太久无意义 — 超时即跳下一个候选
                "timeout": 12.0,
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
            # tool_calls 场景 content 空是合法的; stub/字面 None 视作无效;
            # 另外如果 content 包含 "rejected the request" (llm_call 兜底返回的 HTTP 报错)，说明底层被拒，也视为无效，跳过并换下一个候选
            if (
                (not content.strip() and not tool_calls)
                or content.strip().lower() in ("none", "null")
                or (content.strip() and content.strip() == default and not tool_calls)
                or ("rejected the request (HTTP" in content)
            ):
                last_err = f"{cand_provider}/{cand_model} 返回无效/错误内容: {content!r}"
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
    """veya1.2: opencode-go DeepSeek V4.1 Flash 优先，OpenRouter 依次兜底。"""
    return await _veya12_rr_call(
        messages,
        kwargs,
        pool=_veya12_pool(),
        start=0,
        alias="veya1.2",
        route="opencode-openrouter-ordered",
        pool_label="免费池",
        fallback_reason="veya1.2 opencode-go/OpenRouter pool empty → gpt-5.6-luna",
    )


async def _veya12_free_call(messages: list[dict], kwargs: dict) -> dict:
    """veya-free: eligibility-filtered free/cheap pool with round robin.

    SPEC v1.0 §7: the pool is derived from ``~/.veya/model-state.json`` (the
    single routing authority) rather than a hardcoded model-name priority list,
    so a model only enters the pool once a real probe marked it eligible. The
    static ``_VEYA12_FREE_POOL`` seed and the lifecycle
    ``_replace_veya12_free_pool`` hook are retained as fallbacks (§21).

    ``_VEYA12_FREE_POOL`` holds the gateway's *live* probe results, which are
    fresher than the state file: the lifecycle in ``scripts/veya_llm_gateway.py``
    re-probes every 24h and evicts a model whose endpoint started answering 402
    or 403, while the state file keeps the model ``eligible`` until something
    else rewrites it. Preferring the live pool keeps a request from paying a
    guaranteed-failing round trip against a model the gateway already retired;
    the state-file pool stays the fallback so a cold gateway (no refresh yet)
    still routes.
    """
    global _veya12_free_rr_cursor
    live = list(_VEYA12_FREE_POOL)
    derived = _cp.free_pool_candidates()
    if live:
        pool, source = live, "gateway-probe"
    elif derived:
        pool, source = derived, "model-state.json:eligible"
    else:
        pool, source = list(_VEYA12_FREE_POOL), "static-seed-fallback"
    if not pool:
        fb = await _frontier_fallback(
            messages,
            kwargs,
            reason="veya-free pool empty after eligibility filtering",
        )
        if fb is not None:
            return fb
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "veya-free 池当前没有合格模型 (eligibility 未满足)",
                    }
                }
            ],
            "usage": {},
            "error": True,
        }
    start = _veya12_free_rr_cursor % len(pool)
    _veya12_free_rr_cursor = (_veya12_free_rr_cursor + 1) % len(pool)
    resp = await _veya12_rr_call(
        messages,
        kwargs,
        pool=pool,
        start=start,
        alias="veya-free",
        route="veya-free-rr",
        pool_label="免费池",
        fallback_reason="veya-free pool empty → gpt-5.6-luna",
    )
    resp.setdefault("router", {})["POOL_SOURCE"] = source
    resp["router"]["POOL_SIZE"] = len(pool)
    return resp


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
# veya1.2-128K 长上下文池: 已退役
# ---------------------------------------------------------------------------
# 原池 = 9 个 openrouter :free 文本模型 + 28 个 inferera <512K 小上下文模型。
# inferera 那一半的免费配额 2026-08-30 即已耗尽（见上方 _INFERERA_FREE_MODELS
# 注释），openrouter 那一半在 P15 的 openrouter key 落地前根本拿不到凭据，
# 于是 alias 解析出的必然是一个打不通的池：请求要么落空、要么被 frontier
# 顶替，而 route 标签还写着 "小上下文免费池"。整段删除后 veya1.2-128K 由
# LEGACY_ALIAS_MAP 解析到 veya-free，走 model-state.json 的资格化池（SPEC §5），
# long 任务类不再有假池。
# ---------------------------------------------------------------------------


async def _veya_dp41_jev113_call(messages: list[dict], kwargs: dict) -> dict:
    """veya-dp4.1-jev-1.13: Advisor (jev-1.13-free) + Executor (deepseek-v4.1-flash).

    Advisor (opencode/jev-1.13-free, Zen SystemOne):
      - Read-only: classifies intent, complexity, prunes context, gives micro-plan
      - Outputs structured JSON advisory (≤300 tokens)
      - No tools, no side effects

    Executor (opencode-go/deepseek-v4.1-flash, Zen Go):
      - Final authority, all tools
      - Receives filtered context + advisory
      - Can override advisory if confidence < 0.65 or tool evidence conflicts

    Policy:
      - advisor_on: new_task, context_over_threshold, replan_after_failure, pre_compaction
      - advisor_skip: simple_followup, tool_continuation, deterministic_action
      - advisor_confidence_threshold: 0.65
    """
    # Check if we should skip advisor based on kwargs hints
    skip_advisor = kwargs.get("veya_skip_advisor", False)
    if skip_advisor:
        # Direct to executor
        return await _call_executor_direct(messages, kwargs)

    # Step 1: Call Advisor (jev-1.13-free)
    advisor_kwargs = dict(kwargs)
    advisor_kwargs.update(
        {
            "provider": "opencode",
            "model": "jev-1.13-free",
            "endpoint": "https://opencode.ai/zen/v1",
            "max_tokens": 300,
            "temperature": 0.1,
            "tools": None,  # Advisor has no tools
            "default_content": "{}",
        }
    )
    # Build advisory prompt
    advisory_prompt = _build_advisory_prompt(messages, kwargs)
    advisor_messages = [
        {"role": "system", "content": _ADVISOR_SYSTEM_PROMPT},
        {"role": "user", "content": advisory_prompt},
    ]

    advisor_resp = await llm_call(advisor_messages, **advisor_kwargs)
    advisory = _parse_advisory(advisor_resp)

    # Step 2: Decide whether to use advisory
    confidence = advisory.get("confidence", 0.0)
    if confidence < 0.65:
        logger.info(
            f"veya-dp41-jev113: advisor confidence {confidence:.2f} < 0.65, skipping advisory"
        )
        return await _call_executor_direct(messages, kwargs)

    # Step 3: Filter context based on advisory
    filtered_messages = _filter_context_by_advisory(messages, advisory)

    # Step 4: Call Executor (deepseek-v4.1-flash) with filtered context + advisory
    executor_kwargs = dict(kwargs)
    executor_kwargs.update(
        {
            "provider": "opencode-go",
            "model": "deepseek-v4.1-flash",
            "endpoint": "https://opencode.ai/zen/go/v1",
            "tools": kwargs.get("tools"),  # Executor gets all tools
        }
    )

    # Inject advisory into system prompt
    system_prompt = _build_executor_system_prompt(advisory)
    executor_messages = [
        {"role": "system", "content": system_prompt},
        *filtered_messages,
    ]

    executor_resp = await llm_call(executor_messages, **executor_kwargs)
    # Add routing trace for gateway
    executor_resp["router"] = {
        "route": "veya-dp41-jev113",
        "alias": "veya-dp4.1-jev-1.13",
        "model": "deepseek-v4.1-flash",  # upstream model for gateway routing trace
        "advisor": {"provider": "opencode", "model": "jev-1.13-free"},
        "executor": {"provider": "opencode-go", "model": "deepseek-v4.1-flash"},
        "advisory_confidence": confidence,
        "advisory_intent": advisory.get("intent"),
        "advisory_complexity": advisory.get("complexity"),
        "context_filtered": len(filtered_messages)
        != len([m for m in messages if m.get("role") != "system"]),
    }
    return executor_resp


async def _call_executor_direct(messages: list[dict], kwargs: dict) -> dict:
    """Call executor directly without advisor."""
    executor_kwargs = dict(kwargs)
    executor_kwargs.update(
        {
            "provider": "opencode-go",
            "model": "deepseek-v4.1-flash",
            "endpoint": "https://opencode.ai/zen/go/v1",
            "tools": kwargs.get("tools"),
        }
    )
    resp = await llm_call(messages, **executor_kwargs)
    resp["router"] = {
        "route": "veya-dp41-jev113-direct",
        "alias": "veya-dp4.1-jev-1.13",
        "model": "deepseek-v4.1-flash",
        "advisor": None,
        "executor": {"provider": "opencode-go", "model": "deepseek-v4.1-flash"},
        "advisory_confidence": 0.0,
    }
    return resp


def _build_advisory_prompt(messages: list[dict], kwargs: dict) -> str:
    """Build the prompt for the advisor model."""
    # Extract current user request (last user message)
    user_request = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_request = m.get("content", "")
            break

    # Build context summary (last N messages, truncated)
    context_summary = _summarize_context(messages, max_chars=4000)

    return f"""CURRENT USER REQUEST:
{user_request}

SESSION CONTEXT SUMMARY:
{context_summary}

AVAILABLE TOOLS:
{json.dumps([t.get("function", {}).get("name", "") for t in (kwargs.get("tools") or [])], ensure_ascii=False)}

Analyze and output JSON advisory (max 300 tokens)."""


def _parse_advisory(resp: dict) -> dict:
    """Parse advisory JSON from advisor response."""
    try:
        content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
        # Try to extract JSON from content
        import re

        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                return parsed
    except Exception as e:
        logger.warning(f"veya-dp41-jev113: failed to parse advisory: {e}")
    return {
        "intent": "unknown",
        "complexity": "medium",
        "needs_tools": True,
        "relevant_context": [],
        "ignore_context": [],
        "plan": [],
        "risk": "medium",
        "confidence": 0.0,
    }


def _filter_context_by_advisory(messages: list[dict], advisory: dict) -> list[dict]:
    """Filter messages based on advisory's relevant_context/ignore_context."""
    relevant = advisory.get("relevant_context", [])
    ignore = advisory.get("ignore_context", [])

    # Keep system message
    system_msgs = [m for m in messages if m.get("role") == "system"]
    other_msgs = [m for m in messages if m.get("role") != "system"]

    # Simple filter: if relevant_context specifies keywords, keep messages containing them
    # If ignore_context specifies keywords, drop messages containing them
    filtered = []
    for m in other_msgs:
        content = str(m.get("content", ""))
        if any(kw.lower() in content.lower() for kw in ignore):
            continue
        if relevant and not any(kw.lower() in content.lower() for kw in relevant):
            continue
        filtered.append(m)

    return system_msgs + filtered


def _summarize_context(messages: list[dict], max_chars: int = 4000) -> str:
    """Summarize recent context for advisor."""
    # Take last ~10 messages, truncate each
    recent = messages[-10:]
    parts = []
    total = 0
    for m in recent:
        role = m.get("role", "")
        content = str(m.get("content", ""))[:500]
        part = f"[{role}] {content}"
        if total + len(part) > max_chars:
            break
        parts.append(part)
        total += len(part)
    return "\n".join(parts) if parts else "(no context)"


def _build_executor_system_prompt(advisory: dict) -> str:
    """Build system prompt for executor with advisory injected."""
    advisory_json = json.dumps(advisory, ensure_ascii=False, indent=2)
    return f"""You are the primary executor. You have final authority.

ADVISORY (from context router, confidence={advisory.get("confidence", 0):.2f}):
{advisory_json}

Rules:
- Advisory is guidance, not command. If confidence < 0.65 or tool evidence conflicts, ignore it.
- Execute the plan using available tools.
- Produce the final answer."""


_ADVISOR_SYSTEM_PROMPT = """You are a context router and micro-planner for a coding agent.
Your job is to analyze the user request and session context, then output a concise JSON advisory.

Output format (JSON only, ≤300 tokens):
{
  "intent": "code_fix|refactor|explore|debug|write|review|other",
  "complexity": "low|medium|high",
  "needs_tools": true|false,
  "relevant_context": ["keyword1", "keyword2", ...],
  "ignore_context": ["keyword1", "keyword2", ...],
  "plan": ["step1", "step2", "step3"],
  "risk": "low|medium|high",
  "confidence": 0.0~1.0
}

Rules:
- relevant_context: keywords to KEEP from session history
- ignore_context: keywords to DROP from session history
- plan: 3-5 atomic steps for the executor
- confidence: your certainty this advisory is correct (0.0-1.0)
- Be concise. No prose."""


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
    # ------------------------------------------------------------------
    # SPEC v1.0 §2/§10: canonical proxy dispatch.  Resolves the four stable
    # logical proxies (veya1.2 / veya-free / veya-nim / veya-vl) plus every
    # legacy alias, and stamps the §6 routing decision onto the response.
    # Runs BEFORE the legacy alias branches below so that, e.g., a config
    # injected provider="veya1.2" cannot swallow a requested "veya1.2-free".
    # ------------------------------------------------------------------
    _resolved = _cp.resolve_canonical(model, provider)
    if _resolved is not None:
        _low = _resolved.requested.lower()
        if _resolved.canonical == "veya-nim":
            return await _cp.veya_nim_call(messages, kwargs, _resolved)
        if _resolved.canonical == "veya-vl":
            return await _cp.veya_vl_call(messages, kwargs, _resolved)
        if _resolved.canonical == "veya-free":
            # The veya1.2-128K long-context pool is retired.  Its candidates were
            # Inferera catalog entries whose free quota was exhausted on
            # 2026-08-30, and the only surviving OpenRouter :free entries were
            # unreachable without a key.  The alias still resolves to veya-free
            # (§11) and now answers from the eligibility-filtered pool in
            # model-state.json like every other long request.
            return await _cp.veya_free_call(messages, kwargs, _resolved)
        # canonical == "veya1.2" — master brain, delegates internally
        _resp = await _veya12_flash_call(messages, kwargs)
        _r = _resp.get("router") or {}
        return _cp.stamp(
            _resp,
            requested_proxy=_resolved.requested,
            routed_proxy="veya1.2",
            provider=str(_r.get("provider") or ""),
            model=str(_r.get("model") or ""),
            resolved_upstream=str(_r.get("model") or ""),
            routing_reason=(
                "master brain default"
                if _r.get("route") == "opencode-openrouter-ordered"
                else f"master brain delegation: {_r.get('route') or 'unknown'}"
            ),
            deprecation=_cp.deprecation_for(_resolved),
        )

    # veya-dp4.1-jev-1.13: Advisor (jev-1.13-free) + Executor (deepseek-v4.1-flash)
    if model in ("veya-dp4.1-jev-1.13", "veya-dp4.1-jev-1.13-free") or provider in (
        "veya-dp4.1-jev-1.13",
        "veya-dp4.1-jev-1.13-free",
    ):
        return await _veya_dp41_jev113_call(messages, kwargs)
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
    clients: list[httpx.AsyncClient] = []
    try:
        # AsyncClient owns sockets, SSL contexts, and connection-pool state.
        # Keep construction inside the lifecycle block so partially-created
        # client lists are also cleaned up when the process is FD constrained.
        clients.append(httpx.AsyncClient(timeout=timeout))
        if proxy:
            clients.append(httpx.AsyncClient(timeout=timeout, proxy=proxy))
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
    finally:
        # Do not rely on AsyncClient.__del__: its async transport cannot be
        # deterministically closed by GC, which previously leaked one or more
        # sockets per request and exhausted the gateway's 1024-FD limit.
        if clients:
            await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)


async def llm_stream(messages: list[dict], **kwargs: Any) -> AsyncIterator[dict]:
    """Streaming chat completion (OpenAI delta events), stub fallback."""
    provider, model = get_provider_config(
        kwargs.get("config"), provider=kwargs.get("provider"), model=kwargs.get("model")
    )
    # SPEC v1.0 §2.3: veya-nim is the only canonical proxy with a dedicated
    # stream runner. Legacy "-nv" spellings resolve here too, so they stream
    # from the qualified NIM pool instead of a retired model id.
    _sresolved = _cp.resolve_canonical(model, provider)
    if _sresolved is not None and _sresolved.canonical == "veya-nim":
        async for event in _cp.veya_nim_stream(messages, kwargs, _sresolved):
            yield event
        return
    nim_alias = (model or provider).lower()
    if nim_alias in _NVIDIA_NIM_ALIASES:
        async for event in _nvidia_nim_stream(messages, kwargs, nim_alias):
            yield event
        return
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
