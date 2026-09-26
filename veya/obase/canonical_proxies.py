"""veya/obase/canonical_proxies — the four canonical Veya model proxies.

SPEC v1.0 §2/§10: business code, Supervisor, Planner and the Gateway may only
name four stable logical proxies::

    veya1.2    master brain (delegates, never a fixed upstream model)
    veya-free  free text / code / reason / long / tool pool
    veya-nim   NVIDIA NIM only
    veya-vl    free vision / OCR / document pool

Raw upstream model IDs live in ``~/.veya/model-state.json`` and
``~/.veya/provider-registry.json`` — never in business code.

Legacy aliases stay resolvable (§11) but every substitution is *stamped* into
``resp["router"]`` so nothing is silently swapped (``SILENT_SUBSTITUTION=0``).

Import cycle note: this module needs ``llm_call`` from :mod:`veya.obase.llm`,
while the facade needs :func:`resolve_canonical` from here.  The facade imports
this module at module scope; this module imports the facade lazily inside
functions.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CANONICAL_PROXIES: tuple[str, ...] = ("veya1.2", "veya-free", "veya-nim", "veya-vl")

DEFAULT_PROXY = "veya1.2"

#: Legacy alias -> canonical proxy.  Every key here is a *deprecated* spelling:
#: resolving one is recorded, never silent (§11).  Keys are lower-cased because
#: :func:`resolve_canonical` normalises the requested name before lookup.
LEGACY_ALIAS_MAP: dict[str, str] = {
    # pre-1.2 spellings of the master brain
    "veya1.1": "veya1.2",
    "veya-1.1": "veya1.2",
    "veya1.2-flash": "veya1.2",
    "veya-1.2-flash": "veya1.2",
    # free pool
    "veya1.2-free": "veya-free",
    "veya-1.2-free": "veya-free",
    # long-context pool folded into the free proxy's long class
    "veya1.2-128k": "veya-free",
    "veya-1.2-128k": "veya-free",
    # vision pool
    "veya1.2-vl": "veya-vl",
    "veya-1.2-vl": "veya-vl",
    # NVIDIA NIM aliases — upstream models behind them are retired; the alias
    # now resolves to the veya-nim proxy, never to a dead model id.
    "veya-m3-nv": "veya-nim",
    "veya-deepseek-v4-flash-nv": "veya-nim",
    "veya-qwen3.5-397b-nv": "veya-nim",
    "veya-kimi-k2.6-nv": "veya-nim",
    "veya-glm5.1-nv": "veya-nim",
}

#: Retired alias -> replacement model id, surfaced for operator clarity only.
#: Never used to route.
RETIRED_UPSTREAM: dict[str, str] = {
    "veya-m3-nv": "minimaxai/minimax-m3 (410 EOL 2026-09-09) -> moonshotai/kimi-k3",
    "veya-deepseek-v4-flash-nv": "deepseek-ai/deepseek-v4-flash-0731 (410 EOL 2026-09-21)",
    "veya-qwen3.5-397b-nv": "qwen/qwen3.5-397b-a17b (410 EOL 2026-07-27)",
    "veya-kimi-k2.6-nv": "moonshotai/kimi-k2.6 (404 no entitlement) -> moonshotai/kimi-k3",
    "veya-glm5.1-nv": "z-ai/glm5.1 (404 removed) -> z-ai/glm-5.3",
}

_NIM_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
_NIM_KEY_POOL = Path.home() / ".veya" / "secrets" / "nim-key-pool"
_MODEL_STATE = Path.home() / ".veya" / "model-state.json"
_PROVIDER_REGISTRY = Path.home() / ".veya" / "provider-registry.json"


# ---------------------------------------------------------------------------
# Alias resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedProxy:
    """Outcome of mapping a requested model/provider onto a canonical proxy."""

    requested: str
    canonical: str
    deprecated: bool = False
    retired_upstream: str | None = None

    @property
    def is_canonical(self) -> bool:
        return self.requested == self.canonical


def resolve_canonical(model: str | None, provider: str | None = None) -> ResolvedProxy | None:
    """Map ``(model, provider)`` onto one of the four canonical proxies.

    Returns ``None`` when neither names a canonical proxy or a known legacy
    alias, so the caller can fall through to the generic provider path.

    Resolution order mirrors :func:`veya.obase.llm.llm_call`: the longer
    suffixed spellings are tested before the master brain, otherwise
    ``provider="veya1.2"`` injected by config would swallow ``veya1.2-free``.
    """
    requested = (model or provider or "").strip()
    if not requested:
        return None
    key = requested.lower()
    # the model string wins when it is the more specific of the two
    if model and str(model).strip():
        mkey = str(model).strip().lower()
        if mkey in LEGACY_ALIAS_MAP or mkey in CANONICAL_PROXIES:
            key = mkey
    if key in CANONICAL_PROXIES:
        return ResolvedProxy(requested=requested, canonical=key)
    if key in LEGACY_ALIAS_MAP:
        return ResolvedProxy(
            requested=requested,
            canonical=LEGACY_ALIAS_MAP[key],
            deprecated=True,
            retired_upstream=RETIRED_UPSTREAM.get(key),
        )
    return None


# ---------------------------------------------------------------------------
# NIM key pool (§8) — key health is tracked independently of model health
# ---------------------------------------------------------------------------

_KEY_COOLDOWN_S = 300.0
_KEY_MAX_FAILURES = 3


@dataclass
class KeyState:
    key_id: int
    healthy: bool = True
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    last_success: float | None = None
    last_failure: float | None = None
    request_count: int = 0

    def available(self, now: float) -> bool:
        return self.healthy and now >= self.cooldown_until


@dataclass
class NIMKeyPool:
    """Round-robin over NIM keys with per-key failure accounting (§8.2)."""

    keys: list[str] = field(default_factory=list)
    states: dict[int, KeyState] = field(default_factory=dict)
    _cursor: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def load(cls) -> NIMKeyPool:
        raw = ""
        env = os.environ.get("NIM_KEY_POOL", "").strip()
        if env:
            raw = env
        elif _NIM_KEY_POOL.is_file():
            try:
                raw = _NIM_KEY_POOL.read_text(encoding="utf-8")
            except OSError:
                raw = ""
        keys: list[str] = []
        seen: set[str] = set()
        for candidate in raw.replace(",", "\n").splitlines():
            value = candidate.strip()
            if value and value not in seen:
                keys.append(value)
                seen.add(value)
        pool = cls(keys=keys)
        pool.states = {i + 1: KeyState(key_id=i + 1) for i in range(len(keys))}
        return pool

    def size(self) -> int:
        return len(self.keys)

    def _advance(self, now: float) -> list[int]:
        """Eligible key ids, starting at the rotating cursor."""
        if not self.keys:
            return []
        eligible = [i + 1 for i in range(len(self.keys)) if self.states[i + 1].available(now)]
        if not eligible:
            # every key cooling down -> bounded cooldown escape hatch so a
            # transient burst cannot deadlock the proxy indefinitely
            eligible = sorted(self.states, key=lambda i: self.states[i].cooldown_until)
            for key_id in eligible:
                self.states[key_id].cooldown_until = 0.0
            eligible = list(eligible)
        start = self._cursor % len(self.keys)
        rotated = [((start + off) % len(self.keys)) + 1 for off in range(len(self.keys))]
        return [k for k in rotated if k in eligible]

    def next_keys(self, count: int = 1) -> list[tuple[int, str]]:
        """Up to ``count`` (key_id, key) pairs, cursor-advanced."""
        with self._lock:
            order = self._advance(time.monotonic())
            self._cursor = (self._cursor + 1) % max(1, len(self.keys))
            return [(k, self.keys[k - 1]) for k in order[:count]]

    def mark_success(self, key_id: int) -> None:
        state = self.states.get(key_id)
        if state is None:
            return
        state.consecutive_failures = 0
        state.cooldown_until = 0.0
        state.healthy = True
        state.last_success = time.time()
        state.request_count += 1

    def mark_failure(self, key_id: int, status: int | None) -> str:
        """Apply §8.2 error handling.  Returns the action taken."""
        state = self.states.get(key_id)
        if state is None:
            return "unknown-key"
        state.request_count += 1
        state.last_failure = time.time()
        now = time.monotonic()
        if status in (401, 403):
            state.healthy = False
            state.consecutive_failures += 1
            return "key-unhealthy"
        if status == 429:
            state.cooldown_until = now + _KEY_COOLDOWN_S
            state.consecutive_failures += 1
            return "key-cooldown"
        state.consecutive_failures += 1
        if state.consecutive_failures >= _KEY_MAX_FAILURES:
            state.cooldown_until = now + _KEY_COOLDOWN_S
            state.consecutive_failures = 0
            return "key-cooldown"
        return "retry-next-key"


_pool_lock = threading.Lock()
_nim_pool: NIMKeyPool | None = None


def nim_key_pool() -> NIMKeyPool:
    global _nim_pool
    with _pool_lock:
        if _nim_pool is None:
            _nim_pool = NIMKeyPool.load()
        return _nim_pool


def reset_nim_key_pool() -> None:
    """Test hook — drop the cached pool so the next call re-reads the file."""
    global _nim_pool
    with _pool_lock:
        _nim_pool = None


# ---------------------------------------------------------------------------
# Model state (§4.4 / §5 eligibility)
# ---------------------------------------------------------------------------


def _load_registry() -> dict[str, Any]:
    try:
        if _PROVIDER_REGISTRY.is_file():
            return json.loads(_PROVIDER_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {}


def _load_state() -> dict[str, Any]:
    try:
        if _MODEL_STATE.is_file():
            return json.loads(_MODEL_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {}


def eligible_nim_models() -> list[dict[str, Any]]:
    """NIM **text** models whose full eligibility conjunction holds (§5).

    Scoped to ``canonical_proxy == "veya-nim"`` so a vision model that also
    happens to live on NIM is never pulled into the text proxy — the VL pool
    owns it (§2.3 / §2.4 keep the two pools independent).
    """
    now = time.time()
    out: list[dict[str, Any]] = []
    for entry in (_load_state().get("models") or {}).values():
        if not isinstance(entry, dict):
            continue
        if entry.get("provider") != "nvidia-nim":
            continue
        if entry.get("canonical_proxy") != "veya-nim":
            continue
        if not entry.get("eligible"):
            continue
        if entry.get("cooldown_until"):
            try:
                if time.mktime(time.strptime(str(entry["cooldown_until"]), "%Y-%m-%dT%H:%M:%SZ")) > now:
                    continue
            except (ValueError, OverflowError):
                continue
        if not all(
            entry.get(flag)
            for flag in (
                "discovered",
                "healthy",
                "credentials_valid",
                "endpoint_available",
                "model_available",
            )
        ):
            continue
        out.append(entry)
    out.sort(key=lambda e: e.get("latency_ms") or 10**9)
    return out


def eligible_free_models(capability: str = "text") -> list[dict[str, Any]]:
    """veya-free pool: eligibility-filtered, capability-tagged, latency-ordered.

    SPEC §7 forbids hardcoding a model-name priority list and forbids treating
    ``discovered`` as ``usable``. Ordering factors applied here: eligibility
    first, then capability match, then measured latency.
    """
    now = time.time()
    out: list[dict[str, Any]] = []
    for entry in (_load_state().get("models") or {}).values():
        if not isinstance(entry, dict):
            continue
        if entry.get("provider") not in ("opencode-go", "openrouter", "flatkey", "gmi", "bai"):
            continue
        if entry.get("canonical_proxy") != "veya-free":
            continue
        if not entry.get("eligible"):
            continue
        if entry.get("cooldown_until"):
            try:
                if time.mktime(time.strptime(str(entry["cooldown_until"]), "%Y-%m-%dT%H:%M:%SZ")) > now:
                    continue
            except (ValueError, OverflowError):
                continue
        if not all(
            entry.get(flag)
            for flag in (
                "discovered",
                "healthy",
                "credentials_valid",
                "endpoint_available",
                "model_available",
            )
        ):
            continue
        if capability not in (entry.get("capabilities") or ["text"]):
            continue
        out.append(entry)
    out.sort(key=lambda e: e.get("latency_ms") or 10**9)
    return out


def free_pool_candidates() -> list[dict[str, str]]:
    """veya-free pool shaped for the existing ``_veya12_rr_call`` runner."""
    out: list[dict[str, str]] = []
    for entry in eligible_free_models("text"):
        provider = str(entry.get("provider") or "")
        model = str(entry.get("model_id") or "")
        if not provider or not model:
            continue
        candidate: dict[str, str] = {"provider": provider, "model": model, "source": provider}
        endpoint = entry.get("endpoint")
        if endpoint:
            candidate["endpoint"] = str(endpoint)
        out.append(candidate)
    return out


def eligible_vl_models() -> list[dict[str, Any]]:
    """veya-vl pool: eligibility-filtered models whose vision capability is verified.

    SPEC §9 — a model only enters this pool after a real probe covered
    IMAGE_INPUT / IMAGE_UNDERSTANDING / OCR / MULTIMODAL_REASONING / STREAMING.
    ``vision_verified`` is the gate; ``discovered`` alone is never enough.
    """
    now = time.time()
    out: list[dict[str, Any]] = []
    for entry in (_load_state().get("models") or {}).values():
        if not isinstance(entry, dict):
            continue
        if entry.get("canonical_proxy") != "veya-vl":
            continue
        if not entry.get("eligible") or not entry.get("vision_verified"):
            continue
        if entry.get("cooldown_until"):
            try:
                if time.mktime(time.strptime(str(entry["cooldown_until"]), "%Y-%m-%dT%H:%M:%SZ")) > now:
                    continue
            except (ValueError, OverflowError):
                continue
        if not all(
            entry.get(flag)
            for flag in (
                "discovered",
                "healthy",
                "credentials_valid",
                "endpoint_available",
                "model_available",
            )
        ):
            continue
        out.append(entry)
    out.sort(key=lambda e: e.get("latency_ms") or 10**9)
    return out


def executor_contract(name: str) -> dict[str, Any]:
    """Read an executor/provider execution contract from the registry (SPEC §4.3).

    Business code must not hardcode raw upstream model ids (§10). This is the
    sanctioned place for them: ``~/.veya/provider-registry.json``. An executor's
    own env override still wins — callers apply that on top of what they get
    back from here.
    """
    contracts = (_load_registry().get("executor_contracts") or {})
    if name in (contracts.get("workers") or {}):
        return dict(contracts["workers"][name])
    entry = contracts.get(name)
    return dict(entry) if isinstance(entry, dict) else {}


def executor_model(name: str, default: str = "") -> str:
    contract = executor_contract(name)
    return str(contract.get("model") or default)


def executor_provider(name: str, default: str = "") -> str:
    contract = executor_contract(name)
    return str(contract.get("provider") or default)


# ---------------------------------------------------------------------------
# Router stamping (§6 observability contract)
# ---------------------------------------------------------------------------


def stamp(
    resp: dict,
    *,
    requested_proxy: str,
    routed_proxy: str,
    provider: str,
    model: str,
    resolved_upstream: str,
    routing_reason: str,
    deprecation: dict | None = None,
    route: str | None = None,
) -> dict:
    """Attach the §6 routing decision to a response, then return it.

    ``route`` is also written under the legacy lowercase key alongside the
    §6 uppercase fields, so consumers written against the pre-canonical
    ``resp["router"]["route"]`` shape keep working during the migration
    (§21 — do not delete the old path mid-migration).
    """
    router = resp.setdefault("router", {})
    router.update(
        {
            "REQUESTED_PROXY": requested_proxy,
            "ROUTED_PROXY": routed_proxy,
            "SELECTED_PROVIDER": provider,
            "SELECTED_MODEL": model,
            "RESOLVED_UPSTREAM_MODEL": resolved_upstream,
            "ROUTING_REASON": routing_reason,
            "route": route or f"canonical-{routed_proxy}",
            "alias": requested_proxy,
        }
    )
    if deprecation:
        router["DEPRECATION"] = deprecation
    return resp


def deprecation_for(resolved: ResolvedProxy) -> dict | None:
    if not resolved.deprecated:
        return None
    payload: dict[str, Any] = {
        "requested_alias": resolved.requested,
        "canonical_proxy": resolved.canonical,
        "visible": True,
        "silent_substitution": False,
    }
    if resolved.retired_upstream:
        payload["retired_upstream"] = resolved.retired_upstream
    return payload


# ---------------------------------------------------------------------------
# veya-nim (§2.3 / §8)
# ---------------------------------------------------------------------------

_nim_cursor = 0
_nim_cursor_lock = threading.Lock()


def _veya_nim_pool_error(reason: str, resolved: ResolvedProxy | None = None) -> dict:
    from veya.obase.llm import llm_call as _unused  # noqa: F401  (cycle guard, no call)

    resp: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": f"veya-nim 无可用模型: {reason}",
                }
            }
        ],
        "usage": {},
        "error": True,
    }
    return stamp(
        resp,
        requested_proxy=resolved.requested if resolved else "veya-nim",
        routed_proxy="veya-nim",
        provider="nvidia-nim",
        model="",
        resolved_upstream="",
        routing_reason=f"nim pool ineligible: {reason}",
        deprecation=deprecation_for(resolved) if resolved else None,
    )


def _nim_reason(resolved: ResolvedProxy | None) -> str:
    """Accurate §6 ROUTING_REASON.

    A deprecated alias request is neither an explicit ``veya-nim`` request nor
    a master-brain delegation, and reporting it as one would be exactly the
    kind of false observability §6 forbids.
    """
    if resolved is None:
        return "veya-nim default selection"
    if resolved.requested == "veya-nim":
        return "explicit veya-nim request"
    if resolved.deprecated:
        return f"legacy alias {resolved.requested} remapped to veya-nim"
    return "veya1.2 master-brain delegation to nim"


async def veya_nim_call(
    messages: list[dict],
    kwargs: dict,
    resolved: ResolvedProxy | None = None,
) -> dict:
    """NVIDIA NIM only.  Never mixes in another provider (§2.3)."""
    from veya.obase.llm import llm_call, provider_call

    pool = eligible_nim_models()
    if not pool:
        state = _load_state()
        return _veya_nim_pool_error("no model satisfies the eligibility conjunction", resolved)

    global _nim_cursor
    with _nim_cursor_lock:
        start = _nim_cursor % len(pool)
        _nim_cursor = (_nim_cursor + 1) % len(pool)
    candidates = pool[start:] + pool[:start]

    keys = nim_key_pool()
    if keys.size() == 0:
        return _veya_nim_pool_error("NIM key pool empty", resolved)

    timeout = float(kwargs.get("timeout") or 200.0)
    last_err = ""
    attempted: list[str] = []

    for entry in candidates:
        model_id = str(entry.get("model_id") or "")
        if not model_id:
            continue
        attempted.append(model_id)
        for key_id, key in keys.next_keys(count=min(2, keys.size())):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await provider_call(
                        client,
                        "openai",
                        model=model_id,
                        messages=messages,
                        tools=kwargs.get("tools"),
                        max_tokens=kwargs.get("max_tokens", 4096),
                        temperature=kwargs.get("temperature"),
                        endpoint=_NIM_ENDPOINT,
                        api_key=key,
                        tool_choice=kwargs.get("tool_choice"),
                    )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                action = keys.mark_failure(key_id, status)
                last_err = f"HTTP {status} ({action}): {exc}"
                if status in (401, 403) and keys.size() > 1:
                    continue
                continue
            except (httpx.HTTPError, ValueError) as exc:
                keys.mark_failure(key_id, None)
                last_err = f"{type(exc).__name__}: {exc}"
                continue

            keys.mark_success(key_id)
            msg = (resp.get("choices") or [{}])[0].get("message") or {}
            content = (msg.get("content") or "").strip()
            tool_calls = msg.get("tool_calls") or []
            if not content and not tool_calls:
                last_err = f"{model_id} 返回空内容"
                continue
            stamp(
                resp,
                requested_proxy=resolved.requested if resolved else "veya-nim",
                routed_proxy="veya-nim",
                provider="nvidia-nim",
                model=model_id,
                resolved_upstream=model_id,
                routing_reason=_nim_reason(resolved),
                deprecation=deprecation_for(resolved) if resolved else None,
            )
            resp.setdefault("usage", {})
            resp["router"]["NIM_KEY_ID"] = key_id
            resp["router"]["MODEL_HEALTH_SOURCE"] = "~/.veya/model-state.json"
            return resp

        logger.warning("veya-nim 模型 %s 全部尝试失败: %s", model_id, last_err)

    state = _load_state()
    return _veya_nim_pool_error(
        f"tried {attempted or ['<empty>']}; last error: {last_err or 'unknown'}",
        resolved,
    )


async def veya_nim_stream(
    messages: list[dict],
    kwargs: dict,
    resolved: ResolvedProxy | None = None,
):
    """Streaming counterpart of :func:`veya_nim_call`.

    The first SSE chunk carries the §6 routing decision so a streaming caller
    sees the same observability contract as the non-streaming path.
    """
    from veya.obase.llm import provider_stream

    pool = eligible_nim_models()
    if not pool:
        yield {
            "choices": [{"delta": {"content": "veya-nim 无可用模型: eligibility 未满足"}}],
            "router": {
                "REQUESTED_PROXY": resolved.requested if resolved else "veya-nim",
                "ROUTED_PROXY": "veya-nim",
                "ROUTING_REASON": "nim pool ineligible",
                "ERROR": True,
            },
        }
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        return

    global _nim_cursor
    with _nim_cursor_lock:
        start = _nim_cursor % len(pool)
        _nim_cursor = (_nim_cursor + 1) % len(pool)
    candidates = pool[start:] + pool[:start]

    keys = nim_key_pool()
    if keys.size() == 0:
        yield {
            "choices": [{"delta": {"content": "veya-nim NIM key pool empty"}}],
            "router": {"REQUESTED_PROXY": "veya-nim", "ROUTED_PROXY": "veya-nim", "ERROR": True},
        }
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        return

    timeout = float(kwargs.get("timeout") or 200.0)
    for entry in candidates:
        model_id = str(entry.get("model_id") or "")
        if not model_id:
            continue
        for key_id, key in keys.next_keys(count=min(2, keys.size())):
            emitted = False
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async for event in provider_stream(
                        client,
                        "openai",
                        model=model_id,
                        messages=messages,
                        tools=kwargs.get("tools"),
                        max_tokens=kwargs.get("max_tokens", 4096),
                        endpoint=_NIM_ENDPOINT,
                        api_key=key,
                    ):
                        if not emitted:
                            event = dict(event)
                            event["router"] = {
                                "REQUESTED_PROXY": (
                                    resolved.requested if resolved else "veya-nim"
                                ),
                                "ROUTED_PROXY": "veya-nim",
                                "SELECTED_PROVIDER": "nvidia-nim",
                                "SELECTED_MODEL": model_id,
                                "RESOLVED_UPSTREAM_MODEL": model_id,
                                "ROUTING_REASON": "veya-nim stream selection",
                                "NIM_KEY_ID": key_id,
                            }
                            if resolved and resolved.deprecated:
                                event["router"]["DEPRECATION"] = deprecation_for(resolved)
                            emitted = True
                        yield event
                keys.mark_success(key_id)
                return
            except httpx.HTTPStatusError as exc:
                keys.mark_failure(key_id, exc.response.status_code)
                if emitted:
                    return
                continue
            except (httpx.HTTPError, ValueError) as exc:
                keys.mark_failure(key_id, None)
                logger.warning("veya-nim 流式失败 %s: %s", model_id, exc)
                if emitted:
                    return
                continue

    yield {
        "choices": [{"delta": {"content": "veya-nim 所有候选模型流式调用失败"}}],
        "router": {
            "REQUESTED_PROXY": resolved.requested if resolved else "veya-nim",
            "ROUTED_PROXY": "veya-nim",
            "ERROR": True,
        },
    }
    yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


# ---------------------------------------------------------------------------
# veya-free / veya-vl — delegate to the existing qualified pool runners
# ---------------------------------------------------------------------------

async def veya_free_call(
    messages: list[dict],
    kwargs: dict,
    resolved: ResolvedProxy | None = None,
) -> dict:
    from veya.obase.llm import _veya12_free_call

    requested = resolved.requested if resolved else "veya-free"
    resp = await _veya12_free_call(messages, kwargs)
    router = resp.get("router") or {}
    model = str(router.get("model") or "")
    via_frontier = not model
    return stamp(
        resp,
        requested_proxy=requested,
        routed_proxy="veya-free",
        provider="" if via_frontier else str(router.get("provider") or ""),
        model=model,
        resolved_upstream="" if via_frontier else model,
        routing_reason=(
            "veya-free pool exhausted; answered by the frontier path"
            if via_frontier
            else "veya-free pool selection"
        ),
        route=str(router.get("route") or ("veya-free-frontier" if via_frontier else "veya-free-pool")),
        deprecation=deprecation_for(resolved) if resolved else None,
    )


async def veya_vl_call(
    messages: list[dict],
    kwargs: dict,
    resolved: ResolvedProxy | None = None,
) -> dict:
    """veya-vl: vision/OCR/document pool, eligibility- and probe-filtered.

    Two guarantees this runner exists to provide:

    1. Only models whose vision capability was really probed are eligible (§9).
    2. A frontier text answer is never returned silently. If the VL pool is
       empty or fails, the response says so in ``router``; and when the request
       actually carries an image, no text-only fallback is attempted at all —
       a blind answer is worse than a structured error.
    """
    from veya.obase.llm import _frontier_fallback, _has_visual_content, llm_call

    requested = resolved.requested if resolved else "veya-vl"
    pool = eligible_vl_models()
    has_attachment = _has_visual_content(messages)

    if not pool:
        reason = (
            "no model passed the vision probe (IMAGE_INPUT/OCR/MULTIMODAL/STREAMING)"
        )
        if not has_attachment:
            fb = await _frontier_fallback(messages, kwargs, reason=f"veya-vl: {reason}")
            if fb is not None:
                return stamp(
                    fb,
                    requested_proxy=requested,
                    routed_proxy="veya-vl",
                    provider="frontier-fallback",
                    model=str((fb.get("router") or {}).get("model") or ""),
                    resolved_upstream="text-only (no attachment in request)",
                    routing_reason=f"vl pool empty ({reason}); text-only request answered by frontier",
                    route="veya-vl-frontier-textonly",
                    deprecation=deprecation_for(resolved) if resolved else None,
                )
        return stamp(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": f"veya-vl 无可用视觉模型: {reason}",
                        }
                    }
                ],
                "usage": {},
                "error": True,
            },
            requested_proxy=requested,
            routed_proxy="veya-vl",
            provider="",
            model="",
            resolved_upstream="",
            routing_reason=f"vl pool empty: {reason}",
            route="veya-vl-unavailable",
            deprecation=deprecation_for(resolved) if resolved else None,
        )

    keys = nim_key_pool()
    timeout = float(kwargs.get("timeout") or 200.0)
    last_err = ""

    for entry in pool:
        model_id = str(entry.get("model_id") or "")
        provider = str(entry.get("provider") or "")
        if not model_id or not provider:
            continue
        if provider == "nvidia-nim":
            if keys.size() == 0:
                last_err = "NIM key pool empty"
                continue
            call = await _nim_attempt(
                messages, kwargs, model_id, keys, timeout, last_err
            )
        else:
            call_kwargs: dict[str, Any] = {
                "config": kwargs.get("config"),
                "provider": provider,
                "model": model_id,
                "tools": kwargs.get("tools"),
                "max_tokens": kwargs.get("max_tokens", 4096),
                "timeout": timeout,
            }
            endpoint = entry.get("endpoint")
            if endpoint:
                call_kwargs["endpoint"] = str(endpoint)
            try:
                resp = await llm_call(messages, **call_kwargs)
            except Exception as exc:  # noqa: BLE001 - pool walk, report and move on
                last_err = f"{provider}/{model_id}: {exc}"
                continue
            msg = (resp.get("choices") or [{}])[0].get("message") or {}
            content = (msg.get("content") or "").strip()
            if not content and not msg.get("tool_calls"):
                last_err = f"{provider}/{model_id} returned empty content"
                continue
            call = (resp, None)

        resp, key_id = call
        if resp is None:
            continue
        return stamp(
            resp,
            requested_proxy=requested,
            routed_proxy="veya-vl",
            provider=provider,
            model=model_id,
            resolved_upstream=model_id,
            routing_reason="veya-vl vision pool selection (probe-verified)",
            route="veya-vl-pool",
            deprecation=deprecation_for(resolved) if resolved else None,
        )

    if not has_attachment:
        fb = await _frontier_fallback(messages, kwargs, reason=f"veya-vl pool failed: {last_err}")
        if fb is not None:
            return stamp(
                fb,
                requested_proxy=requested,
                routed_proxy="veya-vl",
                provider="frontier-fallback",
                model=str((fb.get("router") or {}).get("model") or ""),
                resolved_upstream="text-only (no attachment in request)",
                routing_reason=(
                    f"vl pool exhausted ({last_err}); text-only request answered by frontier"
                ),
                route="veya-vl-frontier-textonly",
                deprecation=deprecation_for(resolved) if resolved else None,
            )
    return stamp(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": f"veya-vl 视觉池调用失败: {last_err or 'no eligible model'}",
                    }
                }
            ],
            "usage": {},
            "error": True,
        },
        requested_proxy=requested,
        routed_proxy="veya-vl",
        provider="",
        model="",
        resolved_upstream="",
        routing_reason=f"vl pool exhausted: {last_err or 'no eligible model'}",
        route="veya-vl-failed",
        deprecation=deprecation_for(resolved) if resolved else None,
    )


async def _nim_attempt(
    messages: list[dict],
    kwargs: dict,
    model_id: str,
    keys: NIMKeyPool,
    timeout: float,
    last_err: str,
) -> tuple[dict | None, int | None]:
    """Try one NIM model across the key pool.  Returns (resp, key_id)."""
    from veya.obase.llm import provider_call

    err = last_err
    for key_id, key in keys.next_keys(count=min(2, keys.size())):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await provider_call(
                    client,
                    "openai",
                    model=model_id,
                    messages=messages,
                    tools=kwargs.get("tools"),
                    max_tokens=kwargs.get("max_tokens", 4096),
                    temperature=kwargs.get("temperature"),
                    endpoint=_NIM_ENDPOINT,
                    api_key=key,
                    tool_choice=kwargs.get("tool_choice"),
                )
        except httpx.HTTPStatusError as exc:
            keys.mark_failure(key_id, exc.response.status_code)
            err = f"HTTP {exc.response.status_code}: {exc}"
            continue
        except (httpx.HTTPError, ValueError) as exc:
            keys.mark_failure(key_id, None)
            err = f"{type(exc).__name__}: {exc}"
            continue
        keys.mark_success(key_id)
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        if not (msg.get("content") or "").strip() and not msg.get("tool_calls"):
            err = f"{model_id} returned empty content"
            continue
        resp.setdefault("router", {})["NIM_KEY_ID"] = key_id
        return resp, key_id
    return None, None
