"""SupervisionRouter — AUTO selection and runtime supervisor switching.

AUTO is not a third supervisor: it selects between ``external`` and
``internal`` (spec §11) and may switch at runtime (§13). Every switch is
persisted with lineage on the mission (never a new mission) via :class:`MissionStore`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import LineageEntry, Mission, SupervisionMode
from .policy import fallback_mode, preferred_mode, switch_direction
from .store import MissionStore

_HIGH_RISK = {"high", "critical"}


@dataclass
class RouterRequest:
    mission: Mission
    characteristics: list[str] = field(default_factory=list)
    risk: str = "normal"
    external_available: bool = True
    expected_duration_s: float | None = None
    cost_policy: str = "balanced"


@dataclass
class RouterDecision:
    selected_mode: str
    reason_code: str
    confidence: float
    fallback_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_mode": self.selected_mode,
            "reason_code": self.reason_code,
            "confidence": self.confidence,
            "fallback_mode": self.fallback_mode,
        }


class SupervisionRouter:
    def __init__(self, store: MissionStore | None = None) -> None:
        self.store = store

    # ── initial selection ───────────────────────────────────────────
    def select(self, request: RouterRequest) -> RouterDecision:
        mission = request.mission
        policy = mission.policies.supervisor_policy

        # Explicit user choice wins; only availability can change it.
        if mission.supervision_mode is SupervisionMode.external:
            return self._with_availability(
                str(SupervisionMode.external), "explicit_external", request, policy
            )
        if mission.supervision_mode is SupervisionMode.internal:
            return RouterDecision(
                selected_mode=str(SupervisionMode.internal),
                reason_code="explicit_internal",
                confidence=1.0,
            )

        # AUTO: characteristics + risk bias, then availability.
        bias = preferred_mode(request.characteristics)
        if bias == str(SupervisionMode.internal) and request.risk in _HIGH_RISK:
            return self._with_availability(
                str(SupervisionMode.external), "risk_external", request, policy
            )
        reason = "hint_external" if bias == str(SupervisionMode.external) else "hint_internal"
        return self._with_availability(bias, reason, request, policy)

    def _with_availability(
        self,
        mode: str,
        reason_code: str,
        request: RouterRequest,
        policy: dict[str, Any],
    ) -> RouterDecision:
        if mode == str(SupervisionMode.external) and not request.external_available:
            fallback = fallback_mode(
                str(SupervisionMode.external),
                external_available=False,
                policy=policy,
            )
            if fallback == str(SupervisionMode.internal):
                return RouterDecision(
                    selected_mode=str(SupervisionMode.internal),
                    reason_code="external_unavailable_fallback",
                    confidence=0.6,
                    fallback_mode=str(SupervisionMode.external),
                )
            # Persist WAITING_EXTERNAL_SUPERVISOR; do not silently downgrade.
            return RouterDecision(
                selected_mode=str(SupervisionMode.external),
                reason_code="external_unavailable_wait",
                confidence=0.5,
                fallback_mode=None,
            )
        return RouterDecision(selected_mode=mode, reason_code=reason_code, confidence=0.8)

    # ── runtime switching ───────────────────────────────────────────
    def switch(
        self,
        mission: Mission,
        *,
        current: str,
        trigger: str,
        reason: str = "",
        confidence: float | None = None,
        iteration: int = 0,
    ) -> RouterDecision | None:
        """Switch supervisor for the *same* mission if policy allows."""

        target = switch_direction(trigger, current)
        if target is None or self.store is None:
            return None
        entry = LineageEntry(
            iteration=iteration,
            from_supervisor=current,
            to_supervisor=target,
            reason=reason or trigger,
            trigger=trigger,
            confidence=confidence,
        )
        lineage = list(mission.authority.get("lineage") or [])
        lineage.append(entry.to_dict())
        mission.authority["lineage"] = lineage
        mission.authority["active_supervisor"] = target
        self.store.save(mission)
        self.store.append_event(
            mission.mission_id,
            "SUPERVISOR_SWITCHED",
            {"from": current, "to": target, "trigger": trigger, "reason": entry.reason},
        )
        return RouterDecision(
            selected_mode=target,
            reason_code=f"switch:{trigger}",
            confidence=confidence if confidence is not None else 0.7,
        )

    def lineage(self, mission: Mission) -> list[dict[str, Any]]:
        return list(mission.authority.get("lineage") or [])
