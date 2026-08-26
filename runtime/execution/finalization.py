"""Finalization reserve and deterministic transition guard."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


def calculate_finalization_reserve(
    total_wall_s: float,
    *,
    min_reserve_s: float = 180,
    reserve_ratio: float = 0.15,
    max_reserve_s: float = 900,
) -> float:
    if total_wall_s <= 0:
        return 0.0
    if min_reserve_s < 0 or reserve_ratio < 0 or max_reserve_s < 0:
        raise ValueError("finalization reserve parameters must be non-negative")
    return min(float(max_reserve_s), max(float(min_reserve_s), float(total_wall_s) * reserve_ratio))


@dataclass
class FinalizationController:
    total_wall_s: float
    min_reserve_s: float = 180
    reserve_ratio: float = 0.15
    max_reserve_s: float = 900
    started: bool = False

    @property
    def reserve_s(self) -> float:
        return calculate_finalization_reserve(
            self.total_wall_s,
            min_reserve_s=self.min_reserve_s,
            reserve_ratio=self.reserve_ratio,
            max_reserve_s=self.max_reserve_s,
        )

    def should_start(
        self,
        remaining_wall_s: float,
        *,
        budget_near: bool = False,
        no_progress: bool = False,
        context_near: bool = False,
        operator_stop: bool = False,
    ) -> bool:
        return not self.started and (
            remaining_wall_s <= self.reserve_s
            or budget_near
            or no_progress
            or context_near
            or operator_stop
        )

    def start(self, remaining_wall_s: float, **signals: bool) -> bool:
        if not self.should_start(remaining_wall_s, **signals):
            return False
        self.started = True
        return True


class FinalizationObserver:
    """Observer adapter that turns execution signals into one finalization edge."""

    def __init__(
        self,
        controller: FinalizationController,
        *,
        on_start: Callable[[Any], Any] | None = None,
    ):
        self.controller = controller
        self.on_start = on_start

    @staticmethod
    def _value(ctx: Any, key: str, default: Any = None) -> Any:
        if isinstance(ctx, dict):
            return ctx.get(key, default)
        return getattr(ctx, key, default)

    async def before_round(self, ctx: Any) -> bool:
        """Start finalization when a context reports a terminal signal."""
        remaining = float(self._value(ctx, "remaining_wall_s", 0.0) or 0.0)
        started = self.controller.start(
            remaining,
            budget_near=bool(self._value(ctx, "budget_near", False)),
            no_progress=bool(self._value(ctx, "no_progress", False)),
            context_near=bool(self._value(ctx, "context_near", False)),
            operator_stop=bool(self._value(ctx, "operator_stop", False)),
        )
        if not started:
            return False
        if isinstance(ctx, dict):
            ctx["finalizing"] = True
            ctx["finalization_started"] = True
        else:
            for name in ("finalizing", "finalization_started"):
                if hasattr(ctx, name):
                    setattr(ctx, name, True)
        if self.on_start is not None:
            outcome = self.on_start(ctx)
            if inspect.isawaitable(outcome):
                await outcome
        return True

    async def after_round(self, ctx: Any, outcome: Any) -> Any:
        """Keep the hook shape uniform; finalization owns no semantic outcome."""
        return outcome
