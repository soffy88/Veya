"""Jev decision plane — OpenCode Zen ``state + questions`` fan-out (spec §15/§16).

Authority boundary: Jev classifies, scores, routes and gates. It never writes
code, modifies files, owns a mission, performs final review, declares DONE, or
overrides safety policy. Providers that fail or hit quota fall back to the
supervisor/normal LLM — never silently to a paid model unless explicitly opted in.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from .models import JevAnswer, JevDecision, JevQuestion
from .policy import JevPolicy, policy_from_env, resolve_api_key


class JevUnavailable(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class JevTransport(Protocol):
    async def probe(self) -> set[str]: ...
    async def ask(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class HttpJevTransport:
    """Real Zen transport. ``probe`` lists models; ``ask`` calls /systemone."""

    def __init__(
        self, *, api_key: str, endpoint: str, models_endpoint: str, timeout_s: float
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._models_endpoint = models_endpoint
        self._timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    async def probe(self) -> set[str]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            response = await client.get(self._models_endpoint, headers=self._headers())
        if response.status_code in (401, 403):
            raise JevUnavailable("JEV_PROVIDER_AUTH", "zen rejected the credential")
        response.raise_for_status()
        data = response.json()
        items = data.get("data") if isinstance(data, dict) else None
        return {str(m.get("id")) for m in (items or []) if isinstance(m, dict)}

    async def ask(self, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            response = await client.post(self._endpoint, headers=self._headers(), json=payload)
        if response.status_code in (401, 403):
            raise JevUnavailable("JEV_PROVIDER_AUTH", "zen rejected the credential")
        if response.status_code == 429:
            raise JevUnavailable("JEV_QUOTA", "zen rate limit")
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or "answers" not in data:
            raise JevUnavailable("JEV_ERROR", f"unexpected zen response: {str(data)[:200]}")
        return data


JevFallback = Callable[[dict[str, Any], list[JevQuestion]], Awaitable[JevDecision]]


@dataclass
class JevDecisionPlane:
    policy: JevPolicy
    transport: JevTransport | None
    key_source: str | None = None
    fallback: JevFallback | None = None

    @classmethod
    def create(
        cls,
        *,
        policy: JevPolicy | None = None,
        transport: JevTransport | None = None,
        environ: dict[str, str] | None = None,
        fallback: JevFallback | None = None,
    ) -> JevDecisionPlane:
        resolved = policy or policy_from_env(environ)
        key, source = resolve_api_key(environ)
        built = transport
        if built is None and key:
            built = HttpJevTransport(
                api_key=key,
                endpoint=resolved.endpoint,
                models_endpoint=resolved.models_endpoint,
                timeout_s=resolved.timeout_s,
            )
        return cls(policy=resolved, transport=built, key_source=source, fallback=fallback)

    # ── readiness ───────────────────────────────────────────────────
    async def probe(self) -> dict[str, Any]:
        if not self.policy.enabled:
            return {"available": False, "reason": "JEV_DISABLED", "key_source": self.key_source}
        if self.transport is None:
            return {"available": False, "reason": "JEV_PROVIDER_AUTH", "key_source": None}
        try:
            models = await self.transport.probe()
        except JevUnavailable as exc:
            return {"available": False, "reason": exc.code, "key_source": self.key_source}
        except Exception as exc:  # transport failure -> not available, not a crash
            return {
                "available": False,
                "reason": f"JEV_ERROR:{type(exc).__name__}",
                "key_source": self.key_source,
            }
        has_primary = self.policy.primary_model in models
        return {
            "available": has_primary,
            "reason": None if has_primary else "JEV_MODEL_MISSING",
            "key_source": self.key_source,
            "primary_model": self.policy.primary_model,
            "paid_available": self.policy.paid_model in models,
            "models": len(models),
        }

    # ── decision ────────────────────────────────────────────────────
    async def decide(self, state: dict[str, Any], questions: Iterable[JevQuestion]) -> JevDecision:
        questions = list(questions)
        if not self.policy.enabled:
            raise JevUnavailable("JEV_DISABLED")
        if self.transport is None:
            raise JevUnavailable("JEV_PROVIDER_AUTH", "no zen credential resolved")
        if not questions:
            raise JevUnavailable("JEV_ERROR", "at least one question is required")
        if self.policy.max_calls is not None and self.policy.calls >= self.policy.max_calls:
            raise JevUnavailable("JEV_QUOTA", "jev call budget exhausted")

        base = {
            "state": state,
            "questions": {q.id: q.to_wire() for q in questions},
        }
        last: JevUnavailable | None = None
        for model in self.policy.models():
            self.policy.calls += 1
            try:
                data = await self.transport.ask({**base, "model": model})
            except JevUnavailable as exc:
                last = exc
                continue
            answers = {
                qid: JevAnswer.from_wire(qid, ans)
                for qid, ans in (data.get("answers") or {}).items()
                if isinstance(ans, dict)
            }
            if answers:
                return JevDecision(
                    answers=answers,
                    model=str(data.get("model", model)),
                    cost=data.get("cost"),
                    usage=dict(data.get("usage") or {}),
                    source="jev",
                )
            last = JevUnavailable("JEV_ERROR", "zen returned no answers")
        raise last or JevUnavailable("JEV_ERROR", "no model answered")

    async def decide_or_fallback(
        self, state: dict[str, Any], questions: Iterable[JevQuestion]
    ) -> JevDecision:
        """Jev when available; otherwise the injected supervisor/LLM fallback."""

        questions = list(questions)
        try:
            return await self.decide(state, questions)
        except JevUnavailable:
            if self.fallback is None:
                raise
            decision = await self.fallback(state, questions)
            decision.source = "provider_fallback"
            return decision


__all__ = ["HttpJevTransport", "JevDecisionPlane", "JevFallback", "JevTransport", "JevUnavailable"]
