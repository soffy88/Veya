"""Veya Reach — capability registry + functional provider routing (plane B).

A capability (``github.search``, ``web.read`` …) is served by an ordered list of
providers. Providers are never marked healthy because a binary exists: the
registry only trusts a **functional probe**. Routing records why each provider
was rejected. Credentials are referenced, never stored.

This is the single capability substrate for every L1 worker — no worker keeps
its own web-search stack.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

Prober = Callable[[], Awaitable[tuple[str, str]]]
Invoker = Callable[[dict[str, Any]], Awaitable[Any]]


class CapabilityHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass
class CapabilityProvider:
    provider_id: str
    priority: int
    prober: Prober | None = None
    invoker: Invoker | None = None
    credential_ref: str | None = None
    cost_class: str = "free"
    privacy_class: str = "public"
    health: str = str(CapabilityHealth.UNKNOWN)
    last_probe: float | None = None
    last_success: float | None = None
    failure_reason: str | None = None
    evidence: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "priority": self.priority,
            "health": self.health,
            "credential_ref": self.credential_ref,
            "cost_class": self.cost_class,
            "privacy_class": self.privacy_class,
            "last_probe": self.last_probe,
            "last_success": self.last_success,
            "failure_reason": self.failure_reason,
            "evidence": self.evidence,
        }


@dataclass
class Capability:
    capability_id: str
    providers: list[CapabilityProvider] = field(default_factory=list)

    def ordered(self) -> list[CapabilityProvider]:
        return sorted(self.providers, key=lambda p: p.priority)


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    def register(self, capability_id: str, providers: list[CapabilityProvider]) -> None:
        self._capabilities[capability_id] = Capability(capability_id, list(providers))

    def get(self, capability_id: str) -> Capability | None:
        return self._capabilities.get(capability_id)

    async def probe(self, capability_id: str) -> list[CapabilityProvider]:
        capability = self._capabilities.get(capability_id)
        if capability is None:
            raise CapabilityError("UNKNOWN_CAPABILITY", capability_id)
        for provider in capability.ordered():
            if provider.prober is None:
                provider.health = str(CapabilityHealth.UNKNOWN)
                provider.failure_reason = "NO_PROBER"
                continue
            provider.last_probe = time.time()
            try:
                health, evidence = await provider.prober()
            except Exception as exc:  # a probe failure is not a crash
                health, evidence = str(CapabilityHealth.UNAVAILABLE), f"{type(exc).__name__}: {exc}"
            provider.health = str(health)
            provider.evidence = str(evidence)
            if provider.health == str(CapabilityHealth.HEALTHY):
                provider.last_success = provider.last_probe
                provider.failure_reason = None
            else:
                provider.failure_reason = provider.health
        return capability.ordered()

    async def select(self, capability_id: str) -> tuple[CapabilityProvider | None, dict[str, Any]]:
        """Ordered fallback with routing evidence. Never a binary-exists guess."""

        providers = await self.probe(capability_id)
        rejected: list[dict[str, Any]] = []
        selected: CapabilityProvider | None = None
        for provider in providers:
            if provider.health in {str(CapabilityHealth.HEALTHY), str(CapabilityHealth.DEGRADED)}:
                selected = provider
                break
            rejected.append(
                {"provider_id": provider.provider_id, "reason": provider.failure_reason}
            )
        evidence = {
            "capability": capability_id,
            "selected_provider": selected.provider_id if selected else None,
            "rejected": rejected,
            "health": selected.health if selected else str(CapabilityHealth.UNAVAILABLE),
        }
        return selected, evidence

    async def invoke(self, capability_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Invoke the provider selected by this registry.

        This is deliberately the only runtime bridge. Workers receive the
        normalized result, but never get a provider list or a second router.
        """

        provider, evidence = await self.select(capability_id)
        if provider is None:
            raise CapabilityError("CAPABILITY_UNAVAILABLE", capability_id)
        if provider.invoker is None:
            raise CapabilityError("CAPABILITY_NOT_INVOKABLE", provider.provider_id)
        result = await provider.invoker(dict(request))
        return {
            "capability_id": capability_id,
            "selected_provider": provider.provider_id,
            "health": provider.health,
            "probe_evidence": provider.evidence,
            "routing": evidence,
            "result": result,
        }

    async def doctor(self) -> list[dict[str, Any]]:
        report: list[dict[str, Any]] = []
        for capability_id in self._capabilities:
            selected, evidence = await self.select(capability_id)
            report.append(
                {
                    "capability": capability_id,
                    "selected_provider": evidence["selected_provider"],
                    "health": evidence["health"],
                    "auth_state": (
                        selected.health
                        if selected is not None
                        else next(
                            (
                                p.health
                                for p in self._capabilities[capability_id].ordered()
                                if p.health == str(CapabilityHealth.AUTH_REQUIRED)
                            ),
                            str(CapabilityHealth.UNAVAILABLE),
                        )
                    ),
                    "last_verified": selected.last_probe if selected else None,
                    "repair_hint": (
                        selected.failure_reason if selected else "configure a provider"
                    ),
                }
            )
        return report


class CapabilityError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


__all__ = [
    "Capability",
    "CapabilityError",
    "CapabilityHealth",
    "CapabilityProvider",
    "CapabilityRegistry",
]
