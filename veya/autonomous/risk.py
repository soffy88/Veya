"""Risk Gate and Budget Controller (spec §26, §27).

Invariants:
- High-risk actions cannot bypass policy or proceed without owner escalation.
- Budget consumption is strictly tracked; no unbounded loops until external SIGKILL.
"""

from __future__ import annotations

import time
from typing import Any

from .models import BudgetState, RiskLevel


class AutonomousRiskGate:
    """Evaluates risk levels and gates dangerous actions (spec §27)."""

    def __init__(self):
        self._destructive_commands = {
            "rm -rf /",
            "mkfs",
            "dd",
            "shutdown",
            "reboot",
            "format",
            ":(){ :|:& };:",
        }

    def evaluate(
        self, action_type: str, target: str, payload: dict[str, Any] | None = None
    ) -> RiskLevel:
        p = payload or {}
        cmd = str(p.get("command") or target).strip().lower()
        path = str(p.get("path") or p.get("target") or target).strip().lower()

        # Check production destructive patterns
        if any(d in cmd for d in self._destructive_commands):
            return RiskLevel.REQUIRES_OWNER

        if "rm -rf" in cmd or "drop database" in cmd or "delete from" in cmd or "truncate" in cmd:
            return RiskLevel.REQUIRES_OWNER

        if any(sensitive in path for sensitive in ["/etc/", "/root", "/boot", "id_rsa"]):
            return RiskLevel.REQUIRES_OWNER

        if action_type in {"DEPLOY", "PUBLISH", "PROMOTE_CANONICAL"}:
            return RiskLevel.HIGH

        if action_type in {"WRITE_FILE", "PATCH_FILE", "EXECUTE_SHELL"}:
            return RiskLevel.MEDIUM

        return RiskLevel.LOW

    def assess_action(
        self, action_type: str, payload: dict[str, Any] | None = None
    ) -> tuple[RiskLevel, str]:
        cmd = str((payload or {}).get("command") or "")
        level = self.evaluate(action_type, target=cmd, payload=payload)
        return level, f"Risk evaluated as {level}"

    def requires_escalation(self, level: RiskLevel) -> bool:
        return level in {RiskLevel.HIGH, RiskLevel.REQUIRES_OWNER}


class BudgetController:
    """Enforces compute, execution, and wall-time budgets (spec §26)."""

    def __init__(
        self,
        max_compute: float = 100.0,
        max_executions: int = 50,
        max_wall_time_s: float = 3600.0,
        max_cost: float | None = None,
        max_actions: int | None = None,
    ):
        compute = max_cost if max_cost is not None else max_compute
        executions = max_actions if max_actions is not None else max_executions
        self._initial_executions = executions
        self._consumed_actions = 0
        self._state = BudgetState(
            remaining_compute=compute,
            remaining_execution=executions,
            remaining_wall_time_s=max_wall_time_s,
            retry_count=0,
            provider_availability={},
        )
        self._start_time = time.time()

    def can_execute(self) -> bool:
        return (
            self._state.remaining_execution > 0
            and self._state.remaining_compute > 0
            and self._state.remaining_wall_time_s > 0
        )

    @property
    def action_count(self) -> int:
        return self._consumed_actions

    @property
    def state(self) -> BudgetState:
        elapsed = time.time() - self._start_time
        self._state.remaining_wall_time_s = max(0.0, self._state.remaining_wall_time_s - elapsed)
        self._start_time = time.time()
        return self._state

    def get_budget_state(self) -> BudgetState:
        return self.state

    def consume(
        self,
        compute_units: float = 1.0,
        execution_units: int = 1,
        *,
        action_cost: float | None = None,
        execution_cost: int | None = None,
    ) -> bool:
        cu = action_cost if action_cost is not None else compute_units
        eu = execution_cost if execution_cost is not None else execution_units
        self._state.remaining_compute = max(0.0, self._state.remaining_compute - cu)
        self._state.remaining_execution = max(0, self._state.remaining_execution - eu)
        self._consumed_actions += eu
        return self._state.remaining_execution > 0 and self._state.remaining_compute > 0

    def record_retry(self) -> int:
        self._state.retry_count += 1
        return self._state.retry_count

    def update_provider(self, provider: str, available: bool) -> None:
        self._state.provider_availability[provider] = available
