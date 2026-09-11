"""Health and failover adapter around the existing Provider Router."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass
class ProviderHealth:
    success: int = 0
    timeout: int = 0
    server_error: int = 0
    empty: int = 0
    malformed: int = 0
    failure_streak: int = 0
    last_latency_ms: float = 0.0
    unhealthy: bool = False


class ReliableProviderAdapter:
    """Wrap provider selection/calls without owning GoalRun or context."""

    def __init__(self, provider_router: Any | None = None, *, failure_threshold: int = 2):
        self.provider_router = provider_router
        self.failure_threshold = max(1, failure_threshold)
        self.health: dict[str, ProviderHealth] = {}

    def _health(self, name: str) -> ProviderHealth:
        return self.health.setdefault(name, ProviderHealth())

    def select(
        self, candidates: list[str], requirements: Mapping[str, Any] | None = None
    ) -> list[str]:
        requirements = requirements or {}
        ordered = list(candidates)
        if self.provider_router is not None and hasattr(self.provider_router, "select"):
            selected = self.provider_router.select(candidates, requirements)
            ordered = list(selected or candidates)
        # Router order remains authoritative; unhealthy providers are excluded.
        return [name for name in ordered if not self._health(name).unhealthy]

    @staticmethod
    def _classify(exc: Exception) -> str:
        text = str(exc).lower()
        if isinstance(exc, TimeoutError) or "timeout" in text:
            return "timeout"
        status_code = getattr(exc, "status_code", None)
        if (
            (isinstance(status_code, int) and 500 <= status_code < 600)
            or "5xx" in text
            or "500" in text
            or "server error" in text
        ):
            return "server_error"
        return "malformed"

    async def call(
        self,
        request: Callable[[str], Awaitable[Any]],
        candidates: list[str],
        *,
        requirements: Mapping[str, Any] | None = None,
        goal_run_id: str,
        context: Mapping[str, Any],
    ) -> tuple[str, Any, dict[str, Any]]:
        # These arguments are intentionally required and passed through only
        # as continuity evidence; the adapter never creates a replacement run.
        if not goal_run_id or context is None:
            raise ValueError("same-task identity and context are required")
        last: Exception | None = None
        for name in self.select(candidates, requirements):
            health = self._health(name)
            started = time.perf_counter()
            try:
                result = await request(name)
                if result is None or result == "":
                    health.empty += 1
                    raise ValueError("empty provider response")
                if not isinstance(result, (dict, str)):
                    health.malformed += 1
                    raise ValueError("malformed provider response")
            except Exception as exc:
                last = exc
                kind = self._classify(exc)
                setattr(health, kind, getattr(health, kind) + 1)
                health.failure_streak += 1
                health.unhealthy = health.failure_streak >= self.failure_threshold
                health.last_latency_ms = (time.perf_counter() - started) * 1000
                continue
            health.success += 1
            health.failure_streak = 0
            health.unhealthy = False
            health.last_latency_ms = (time.perf_counter() - started) * 1000
            return name, result, {"goal_run_id": goal_run_id, "context": dict(context)}
        raise RuntimeError(f"all providers failed: {last}")
