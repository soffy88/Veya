"""MissionLoop — the ONE durable loop every supervision mode shares (spec §5/§19).

Three modes, one loop. It is restart/resume/reconnect safe because every step is
persisted before the next side effect:

* the ExecutionReport is written before any review, and a re-run for an
  iteration that already has a report does NOT execute again (no duplicate
  mutation on restart);
* the active supervisor + lineage live on the mission document;
* external mode parks at WAITING_EXTERNAL_SUPERVISOR and resumes when the
  supervisor applies a review — no long-lived ChatGPT daemon required.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from .evidence import build_execution_report
from .external import ExternalSupervisor, MissionRunner
from .models import ExecutionReport, MissionStatus, SupervisionMode
from .reap import reap_orphans
from .retask import apply_review
from .reviewer import InternalSupervisor, SupervisorUnavailable
from .router import RouterRequest, SupervisionRouter
from .runner import executor_hint as _executor_hint
from .store import MissionStore

# Durable execution-handle states (persisted in the same mission store).
STATE_RUNNING = "RUNNING"
STATE_COMPLETED = "COMPLETED"
STATE_INTERRUPTED = "INTERRUPTED"

_TERMINAL = {
    MissionStatus.done,
    MissionStatus.accepted,
    MissionStatus.failed,
    MissionStatus.cancelled,
}
_WAITING = {
    MissionStatus.waiting_external_supervisor,
    MissionStatus.waiting_owner,
}


@dataclass
class MissionLoop:
    store: MissionStore
    router: SupervisionRouter
    runner: MissionRunner
    internal: InternalSupervisor | None = None
    jev: Any = None
    external_available: bool = True
    _external: ExternalSupervisor | None = field(default=None, repr=False)

    def facade(self) -> ExternalSupervisor:
        if self._external is None:
            self._external = ExternalSupervisor(
                store=self.store, router=self.router, runner=self.runner
            )
        return self._external

    # ── one iteration ───────────────────────────────────────────────
    # ── durable execution handle + restart reconciliation ───────────
    def _settle_completed_handle(
        self, mission: Any, iteration: int, report: ExecutionReport
    ) -> None:
        """A report exists; make sure its handle is closed (crash between the two writes)."""
        handle = self.store.execution_for(mission.mission_id, iteration)
        if handle and str(handle.get("state")) == STATE_RUNNING:
            self.store.append_execution(
                mission.mission_id,
                {
                    "execution_id": handle.get("execution_id"),
                    "iteration": iteration,
                    "executor": handle.get("executor"),
                    "attempt": handle.get("attempt", 1),
                    "started_at": handle.get("started_at"),
                    "goalrun_id": report.goalrun_id or handle.get("goalrun_id") or "",
                    "last_checkpoint": report.checkpoint_id or "",
                    "state": STATE_COMPLETED,
                    "reconciled": "report_already_persisted",
                },
            )

    def _interrupted_report(
        self, mission: Any, iteration: int, handle: dict[str, Any], execution_id: str
    ) -> ExecutionReport:
        """Explicit failure for an execution whose result was lost on restart.

        Never a success, never a silent re-dispatch: re-running could duplicate
        side effects, so the retry is an explicit, auditable decision.
        """
        report = ExecutionReport(
            mission_id=mission.mission_id,
            iteration=iteration,
            objective=mission.goal,
            status=STATE_INTERRUPTED,
            failures=[
                {
                    "kind": "execution_interrupted",
                    "execution_id": execution_id,
                    "executor": handle.get("executor"),
                    "started_at": handle.get("started_at"),
                    "reason": "previous execution never produced a report (process restart)",
                }
            ],
            blocked_items=[
                {"reason": "previous execution result unknown; explicit retry required"}
            ],
            runtime_evidence=[
                {
                    "source": "reconciliation",
                    "execution_id": execution_id,
                    "executor": handle.get("executor"),
                    "state": STATE_INTERRUPTED,
                    "action": "no silent re-dispatch (duplicate side effects avoided)",
                }
            ],
            executor_summary=(
                "Execution interrupted by a restart; result unknown - not a success."
            ),
            proposed_next_action="retry",
        )
        # Canonical lifecycle: terminate the process group this execution owns.
        # The cwd/cmdline scanner stays only as a legacy fallback for executions
        # started before group ownership existed.
        from server import exec_process

        pidfile = exec_process.pidfile_for(self.store.mission_dir(mission.mission_id), iteration)
        proc_record = exec_process.read(pidfile) or {}
        term = exec_process.terminate(pidfile, str(mission.workspace or ""))
        reaped = reap_orphans(str(mission.workspace or ""), str(handle.get("executor") or ""))
        if term.get("killed") or reaped:
            report.runtime_evidence.append(
                {
                    "source": "reconciliation",
                    "kind": "executor_group_terminated",
                    "pgid": handle.get("pgid") or proc_record.get("pgid"),
                    "killed": term.get("killed", 0),
                    "pids": term.get("pids", []),
                    "refused": term.get("refused"),
                    "legacy_scanner_pids": reaped,
                }
            )
            self.store.append_event(
                mission.mission_id,
                "EXECUTION_PROCESS_GROUP_TERMINATED",
                {"execution_id": execution_id, **term, "legacy_scanner_pids": reaped},
            )
        self.store.append_execution(
            mission.mission_id,
            {
                "mission_id": mission.mission_id,
                "execution_id": execution_id,
                "iteration": iteration,
                "executor": handle.get("executor"),
                "pid": handle.get("pid") or proc_record.get("pid"),
                "pgid": handle.get("pgid") or proc_record.get("pgid"),
                "started_at": handle.get("started_at"),
                "attempt": handle.get("attempt", 1),
                "state": STATE_INTERRUPTED,
                "terminated": term,
            },
        )
        self.store.append_report(report)
        return report

    async def _dispatch_iteration(self, mission: Any, iteration: int) -> ExecutionReport:
        """Run one iteration behind a durable handle (written *before* the executor)."""
        mission_id = mission.mission_id
        execution_id = f"{mission_id}:{iteration}"
        handle = self.store.execution_for(mission_id, iteration)

        if handle and str(handle.get("state")) == STATE_RUNNING:
            self.store.append_execution(
                mission_id,
                {
                    "execution_id": execution_id,
                    "iteration": iteration,
                    "executor": handle.get("executor"),
                    "attempt": handle.get("attempt", 1),
                    "started_at": handle.get("started_at"),
                    "state": STATE_INTERRUPTED,
                },
            )
            self.store.append_event(
                mission_id,
                "EXECUTION_INTERRUPTED",
                {"iteration": iteration, "execution_id": execution_id},
            )
            return self._interrupted_report(mission, iteration, handle, execution_id)

        attempt = int((handle or {}).get("attempt", 0)) + 1
        executor = _executor_hint(mission)
        from server import exec_process

        pidfile = exec_process.pidfile_for(self.store.mission_dir(mission_id), iteration)
        started_at = time.time()
        mission.status = MissionStatus.executing
        # Project execution_id to mission authority for external visibility
        mission.authority["execution_id"] = execution_id
        mission.authority["iteration"] = iteration
        self.store.save(mission)
        self.store.append_execution(
            mission_id,
            {
                "mission_id": mission_id,
                "execution_id": execution_id,
                "iteration": iteration,
                "executor": executor,
                "goalrun_id": mission.authority.get("goalrun_id") or "",
                "started_at": started_at,
                "state": STATE_RUNNING,
                "attempt": attempt,
            },
        )
        self.store.append_event(
            mission_id,
            "EXECUTOR_STARTED",
            {
                "iteration": iteration,
                "execution_id": execution_id,
                "executor": executor,
                "attempt": attempt,
            },
        )

        previous_pidfile = os.environ.get(exec_process.PIDFILE_ENV)
        os.environ[exec_process.PIDFILE_ENV] = str(pidfile)
        try:
            state = await self.runner(mission)
        finally:
            if previous_pidfile is None:
                os.environ.pop(exec_process.PIDFILE_ENV, None)
            else:
                os.environ[exec_process.PIDFILE_ENV] = previous_pidfile
        proc_record = exec_process.read(pidfile) or {}

        report = build_execution_report(mission, state, iteration=iteration, objective=mission.goal)
        report.runtime_evidence.insert(
            0,
            {
                "source": "execution_handle",
                "execution_id": execution_id,
                "executor": executor,
                "attempt": attempt,
                "pid": proc_record.get("pid"),
                "pgid": proc_record.get("pgid"),
            },
        )
        self.store.append_report(report)
        self.store.append_execution(
            mission_id,
            {
                "mission_id": mission_id,
                "execution_id": execution_id,
                "iteration": iteration,
                "executor": executor,
                "attempt": attempt,
                "pid": proc_record.get("pid"),
                "pgid": proc_record.get("pgid"),
                "started_at": started_at,
                "finished_at": time.time(),
                "goalrun_id": report.goalrun_id or "",
                "last_checkpoint": report.checkpoint_id or "",
                "state": STATE_COMPLETED,
            },
        )
        # Project execution_id and goalrun_id to mission authority
        mission.authority["execution_id"] = execution_id
        mission.authority["iteration"] = iteration
        if report.goalrun_id:
            mission.authority["goalrun_id"] = report.goalrun_id
        self.store.save(mission)
        self.store.append_event(
            mission_id, "EXECUTOR_COMPLETED", {"iteration": iteration, "execution_id": execution_id}
        )
        return report

    async def step(self, mission_id: str) -> dict[str, Any]:
        mission = self.store.load(mission_id)
        if mission is None:
            raise KeyError(f"unknown mission: {mission_id}")
        if mission.status in _TERMINAL:
            return self._snapshot(mission_id, "terminal")

        supervisor = mission.authority.get("active_supervisor")
        if not supervisor:
            decision = self.router.select(
                RouterRequest(
                    mission=mission,
                    characteristics=list(
                        mission.policies.supervisor_policy.get("characteristics") or []
                    ),
                    external_available=self.external_available,
                )
            )
            supervisor = (
                decision.selected_mode
                if mission.supervision_mode is SupervisionMode.auto
                else str(mission.supervision_mode)
            )
            mission.authority["active_supervisor"] = supervisor
            self.store.append_event(
                mission_id,
                "SUPERVISOR_SELECTED",
                {"mode": supervisor, "reason": decision.reason_code},
            )
            self.store.save(mission)

        iteration = int(mission.authority.get("iteration", 0))

        # 1) execute only if this iteration has no report yet (restart-safe).
        report = self.store.get_report(mission_id, iteration)
        if report is None:
            report = await self._dispatch_iteration(mission, iteration)
        else:
            self._settle_completed_handle(mission, iteration, report)

        # 1b) Jev fast decisions (advisory only; a provider failure must not
        # break the loop, and a low-confidence critical call escalates ownership).
        if self.jev is not None:
            try:
                from veya.decision import needs_supervisor
                from veya.decision.questions import standard_questions

                jev = await self.jev.decide({"report": report.to_dict()}, standard_questions())
                report.jev_decisions.append(jev.to_dict())
                self.store.append_report(report)
                self.store.append_event(mission_id, "JEV_DECISION", jev.to_dict())
                if (
                    needs_supervisor(jev)
                    and mission.supervision_mode is SupervisionMode.auto
                    and self.external_available
                    and supervisor != str(SupervisionMode.external)
                ):
                    switched = self.router.switch(
                        mission,
                        current=supervisor,
                        trigger="jev_low_confidence_critical",
                        reason="jev confidence below threshold",
                        confidence=jev.confidence,
                        iteration=iteration,
                    )
                    if switched is not None:
                        mission.status = MissionStatus.waiting_external_supervisor
                        self.store.save(mission)
                        return self._snapshot(mission_id, "jev_escalated")
            except Exception:
                pass

        # 2) external supervision parks, waiting to be resumed by a review.
        if supervisor == str(SupervisionMode.external):
            mission.status = MissionStatus.waiting_external_supervisor
            self.store.save(mission)
            return self._snapshot(mission_id, "waiting_external_supervisor")

        # 3) internal supervision closes the loop on its own.
        if self.internal is None:
            mission.status = MissionStatus.reviewing
            self.store.save(mission)
            return self._snapshot(mission_id, "reviewing_no_supervisor")
        try:
            review = await self.internal.review(mission, report)
        except SupervisorUnavailable:
            mission.status = MissionStatus.waiting_external_supervisor
            self.store.save(mission)
            return self._snapshot(mission_id, "internal_unavailable")

        self.store.append_event(
            mission_id,
            "REVIEW_COMPLETED",
            {"supervisor": "internal", "decision": str(review.decision)},
        )
        # low-confidence internal review in AUTO escalates ownership (spec §13/§25)
        if (
            mission.supervision_mode is SupervisionMode.auto
            and review.confidence is not None
            and review.confidence < 0.5
            and self.external_available
        ):
            switched = self.router.switch(
                mission,
                current=str(SupervisionMode.internal),
                trigger="jev_low_confidence_critical",
                reason="internal review confidence below threshold",
                confidence=review.confidence,
                iteration=iteration,
            )
            if switched is not None:
                mission.status = MissionStatus.waiting_external_supervisor
                self.store.save(mission)
                return self._snapshot(mission_id, "switched_to_external")

        outcome = apply_review(self.store, mission, review, iteration=iteration, report=report)
        if outcome.next_task is not None:
            fresh = self.store.load(mission_id)
            if fresh is not None:
                fresh.authority["iteration"] = iteration + 1
                self.store.save(fresh)
        return self._snapshot(mission_id, str(outcome.mission_status))

    # ── resume / run ────────────────────────────────────────────────
    async def resume(self, mission_id: str) -> dict[str, Any]:
        """Recover from persisted state and take the next step."""

        mission = self.store.load(mission_id)
        if mission is None:
            raise KeyError(f"unknown mission: {mission_id}")
        if mission.status is MissionStatus.waiting_external_supervisor:
            # The external supervisor applies the review out-of-band
            # (veya.review.apply); until then we simply stay parked.
            review = self.store.latest_review(mission_id)
            iteration = int(mission.authority.get("iteration", 0))
            if review is None or review.iteration != iteration:
                return self._snapshot(mission_id, "still_waiting")
        return await self.step(mission_id)

    async def run_to_completion(self, mission_id: str, *, max_steps: int = 20) -> dict[str, Any]:
        """Drive the loop until terminal/waiting or the step bound is hit."""

        for _ in range(max_steps):
            mission = self.store.load(mission_id)
            if mission is None:
                raise KeyError(f"unknown mission: {mission_id}")
            if mission.status in _TERMINAL or mission.status in _WAITING:
                break
            await self.step(mission_id)
        return self._snapshot(mission_id, "run_complete")

    # ── snapshot ────────────────────────────────────────────────────
    def _snapshot(self, mission_id: str, reason: str) -> dict[str, Any]:
        mission = self.store.load(mission_id)
        report = self.store.latest_report(mission_id)
        review = self.store.latest_review(mission_id)
        return {
            "mission_id": mission_id,
            "status": str(mission.status) if mission else None,
            "supervisor": mission.authority.get("active_supervisor") if mission else None,
            "iteration": int(mission.authority.get("iteration", 0)) if mission else None,
            "lineage": self.router.lineage(mission) if mission else [],
            "report": report.to_dict() if report else None,
            "review": review.to_dict() if review else None,
            "reason": reason,
        }


__all__ = ["MissionLoop"]
