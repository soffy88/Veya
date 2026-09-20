"""ExternalSupervisor — the ChatGPT-facing supervisor surface (spec §8/§20).

Reconnect semantics without a ChatGPT daemon:

    iteration completes -> ExecutionReport persisted -> WAITING_EXTERNAL_SUPERVISOR
    ChatGPT reconnects  -> reads latest report -> review.apply(...) -> runtime resumes

Internal/AUTO missions never park here; the selected supervisor keeps the loop
closed on its own. This module owns only orchestration metadata: execution is
delegated to the injected canonical runner (GoalRun / project_ask).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .evidence import build_execution_report
from .models import (
    ExecutionReport,
    Mission,
    MissionStatus,
    SupervisionMode,
    SupervisorReview,
)
from .retask import RetaskOutcome
from .retask import apply_review as _apply_review
from .router import RouterRequest, SupervisionRouter
from .store import MissionStore

MissionRunner = Callable[[Mission], Awaitable[Any]]


class MissionNotFound(KeyError):
    pass


@dataclass
class ExternalSupervisor:
    store: MissionStore
    router: SupervisionRouter
    runner: MissionRunner | None = None
    _started_at: float = field(default_factory=time.time)

    # ── mission lifecycle ───────────────────────────────────────────
    def create(
        self,
        *,
        goal: str,
        supervision_mode: str = "auto",
        workspace: str = "",
        acceptance_criteria: list[str] | None = None,
        constraints: list[str] | None = None,
        characteristics: list[str] | None = None,
        mission_id: str | None = None,
        executor: str | None = None,
    ) -> Mission:
        mid = mission_id or f"mission-{int(time.time() * 1000):x}"
        mission = Mission(
            mission_id=mid,
            goal=goal,
            supervision_mode=SupervisionMode(supervision_mode),
            workspace=workspace,
            acceptance_criteria=list(acceptance_criteria or []),
            constraints=list(constraints or []),
        )
        if characteristics:
            mission.policies.supervisor_policy["characteristics"] = list(characteristics)
        if executor:
            mission.policies.execution_policy["assignee_hint"] = str(executor)
        self.store.save(mission)
        self.store.append_event(mid, "MISSION_CREATED", {"mode": str(mission.supervision_mode)})
        return mission

    def require(self, mission_id: str) -> Mission:
        mission = self.store.load(mission_id)
        if mission is None:
            raise MissionNotFound(mission_id)
        return mission

    def inspect(self, mission_id: str) -> dict[str, Any]:
        mission = self.require(mission_id)
        report = self.store.latest_report(mission_id)
        review = self.store.latest_review(mission_id)
        return {
            "mission": mission.to_dict(),
            "current_supervisor": mission.authority.get("active_supervisor")
            or str(mission.supervision_mode),
            "lineage": self.router.lineage(mission),
            "latest_report": report.to_dict() if report else None,
            "latest_review": review.to_dict() if review else None,
        }

    # ── execution ───────────────────────────────────────────────────
    async def run(self, mission_id: str) -> dict[str, Any]:
        mission = self.require(mission_id)
        external_available = bool(
            mission.policies.supervisor_policy.get("external_available", True)
        )
        decision = self.router.select(
            RouterRequest(
                mission=mission,
                characteristics=list(
                    mission.policies.supervisor_policy.get("characteristics") or []
                ),
                external_available=external_available,
            )
        )
        mission.authority["active_supervisor"] = (
            str(mission.supervision_mode)
            if mission.supervision_mode is not SupervisionMode.auto
            else decision.selected_mode
        )
        mission.status = MissionStatus.executing
        self.store.save(mission)
        self.store.append_event(
            mission_id,
            "SUPERVISOR_SELECTED",
            {"mode": decision.selected_mode, "reason": decision.reason_code},
        )
        self.store.append_event(mission_id, "EXECUTOR_STARTED", {})

        if self.runner is None:
            mission.status = MissionStatus.blocked
            self.store.save(mission)
            return {"status": str(mission.status), "reason": "no execution runner configured"}

        goalrun_state = await self.runner(mission)
        iteration = int(mission.authority.get("iteration", 0))
        report = build_execution_report(
            mission, goalrun_state, iteration=iteration, objective=mission.goal
        )
        self.store.append_report(report)
        self.store.append_event(mission_id, "EXECUTOR_COMPLETED", {"iteration": iteration})

        # Project execution_id to mission authority for external visibility
        execution_id = f"{mission_id}:{iteration}"
        mission.authority["execution_id"] = execution_id
        mission.authority["iteration"] = iteration
        if report.goalrun_id:
            mission.authority["goalrun_id"] = report.goalrun_id
        self.store.save(mission)

        supervisor = mission.authority["active_supervisor"]
        if supervisor == str(SupervisionMode.external):
            mission.status = MissionStatus.waiting_external_supervisor
        else:
            mission.status = MissionStatus.reviewing
        self.store.save(mission)
        return {
            "status": str(mission.status),
            "supervisor": supervisor,
            "report": report.to_dict(),
        }

    # ── review / retask ─────────────────────────────────────────────
    def apply_review(self, mission_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        mission = self.require(mission_id)
        review = SupervisorReview.from_dict({"mission_id": mission_id, **payload})
        if not review.supervisor:
            review.supervisor = str(
                mission.authority.get("active_supervisor") or mission.supervision_mode
            )
        report = self.store.latest_report(mission_id)
        iteration = int(mission.authority.get("iteration", 0))
        outcome: RetaskOutcome = _apply_review(
            self.store, mission, review, iteration=iteration, report=report
        )
        if outcome.next_task is not None:
            mission = self.require(mission_id)
            mission.authority["iteration"] = iteration + 1
            self.store.save(mission)
        return {
            "status": str(outcome.mission_status),
            "reason": outcome.reason,
            "next_task": outcome.next_task.to_dict() if outcome.next_task else None,
            "escalation_code": str(outcome.escalation_code) if outcome.escalation_code else None,
        }

    def cancel(self, mission_id: str) -> dict[str, Any]:
        mission = self.require(mission_id)
        mission.status = MissionStatus.cancelled
        self.store.save(mission)
        self.store.append_event(mission_id, "MISSION_CANCELLED", {})
        return {"status": str(mission.status)}

    # ── reports / escalations ───────────────────────────────────────
    def latest_report(self, mission_id: str) -> dict[str, Any] | None:
        report = self.store.latest_report(mission_id)
        return report.to_dict() if report else None

    def get_report(self, mission_id: str, iteration: int) -> dict[str, Any] | None:
        report: ExecutionReport | None = self.store.get_report(mission_id, iteration)
        return report.to_dict() if report else None

    def list_escalations(self, mission_id: str) -> list[dict[str, Any]]:
        return [e for e in self.store.events(mission_id) if e.get("topic") == "ESCALATED"]

    def artifact_ref(self, mission_id: str, iteration: int | None = None) -> list[dict[str, Any]]:
        report = (
            self.store.get_report(mission_id, iteration)
            if iteration is not None
            else self.store.latest_report(mission_id)
        )
        return list(report.artifacts) if report else []


__all__ = ["ExternalSupervisor", "MissionNotFound", "MissionRunner"]
