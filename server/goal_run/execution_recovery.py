"""ExecutionRecovery — restart / crash reconciliation for Local2 (P5).

GoalRun remains the sole durable execution authority. This module only reads
durable state, probes the provider, and writes a recovery verdict back into
the existing GoalRun / ExecutorBinding / ExecutionScope / ResourceLease
records. It never creates a second durable authority and never launches a
second execution.

Hard invariant: recovery never reports success. A run whose provider,
binding or process evidence is missing becomes ``RECOVERY_REQUIRED`` (or
``LOST`` / ``FAILED`` with explicit evidence) — never ``COMPLETED``.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from server.events import EventEnvelope, event_store
from server.goal_run.execution_admission import (
    _TERMINAL_GOAL_STATUSES,
    BindingStatus,
    _load_binding_records,
    get_admission,
    restore_scopes,
)
from server.goal_run.models import GoalRunState, GoalStatus
from server.goal_run.store import append_event, load_goal_run, save_goal_run

logger = logging.getLogger("veya.goal_run.recovery")


class RecoveryOutcome(StrEnum):
    RESUMABLE = "RESUMABLE"
    RUNNING = "RUNNING"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    LOST = "LOST"
    FAILED = "FAILED"


#: GoalRun statuses that represent live (non-terminal) execution work.
_LIVE_STATUSES = frozenset(
    {
        GoalStatus.running,
        GoalStatus.recovering,
        GoalStatus.paused,
        GoalStatus.resumable,
    }
)

_GOAL_RUNS_DIR = ".veya-project/goal-runs"


@dataclass
class RecoveryVerdict:
    goal_run_id: str
    outcome: RecoveryOutcome
    binding_id: str | None = None
    executor_id: str | None = None
    reason: str = ""
    provider_reachable: bool | None = None
    binding_present: bool = False
    lease_restored: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_run_id": self.goal_run_id,
            "outcome": self.outcome.value,
            "binding_id": self.binding_id,
            "executor_id": self.executor_id,
            "reason": self.reason,
            "provider_reachable": self.provider_reachable,
            "binding_present": self.binding_present,
            "lease_restored": self.lease_restored,
            "evidence": self.evidence,
        }


def _emit_recovery_event(
    project_root: str,
    goal_run_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Record one recovery evidence event (canonical store + GoalRun log)."""
    now = time.time()
    envelope = EventEnvelope(
        event_id=f"rec_{goal_run_id}_{uuid.uuid4().hex[:8]}",
        trace_id=goal_run_id,
        session_id="execution_recovery",
        task_id=None,
        turn_id=None,
        topic=f"execution.recovery.{event_type.lower()}",
        ts=now,
        actor="execution_recovery",
        payload={"goal_run_id": goal_run_id, "event_type": event_type, "payload": payload},
        schema_version=1,
    )
    event_store.append(envelope)
    if goal_run_id == "system":
        # Project-level recovery fact (scope/lease reconcile) has no GoalRun.
        return
    append_event(
        project_root,
        goal_run_id,
        {"type": f"execution_recovery.{event_type}", **payload},
    )


class ExecutionRecovery:
    """Restart/crash reconciliation over the canonical GoalRun records."""

    def __init__(self, project_root: str) -> None:
        self.project_root = os.path.realpath(project_root)
        self.admission = get_admission(project_root)

    # ── Discovery ───────────────────────────────────────────────────

    def discover(self) -> list[str]:
        """Return GoalRun ids currently in a live (non-terminal) status."""
        root = Path(self.project_root) / _GOAL_RUNS_DIR
        if not root.is_dir():
            return []
        found: list[str] = []
        for taskgraph in sorted(root.glob("*/taskgraph.json")):
            state = load_goal_run(self.project_root, taskgraph.parent.name)
            if state is None:
                continue
            if state.status in _LIVE_STATUSES:
                found.append(state.goal_id)
        return found

    # ── Reconcile ───────────────────────────────────────────────────

    async def reconcile(self, goal_run_id: str) -> RecoveryVerdict:
        """Reconcile one GoalRun against durable binding/provider evidence."""
        state = load_goal_run(self.project_root, goal_run_id)
        if state is None:
            return RecoveryVerdict(
                goal_run_id=goal_run_id,
                outcome=RecoveryOutcome.LOST,
                reason="goal_run_record_missing",
            )

        _emit_recovery_event(
            self.project_root, goal_run_id, "RECOVERY_STARTED", {"status": state.status.value}
        )

        checkpoint = state.runtime_checkpoint or {}
        binding_id = str(checkpoint.get("binding_id") or "")

        if not binding_id:
            # Execution identity was never bound. This is the pre-admission
            # crash window: no execution to recover, and it must not be
            # mistaken for live work.
            return await self._finalize(
                state,
                RecoveryOutcome.RECOVERY_REQUIRED,
                reason="binding_missing",
                binding_id=None,
                executor_id=state.requested_executor,
                provider_reachable=None,
                binding_present=False,
            )

        binding = self._load_binding(binding_id)
        if binding is None:
            return await self._finalize(
                state,
                RecoveryOutcome.RECOVERY_REQUIRED,
                reason="binding_record_missing",
                binding_id=binding_id,
                executor_id=state.requested_executor,
                provider_reachable=None,
                binding_present=False,
            )

        executor_id = str(binding.get("executor_id") or state.requested_executor or "")
        _emit_recovery_event(
            self.project_root,
            goal_run_id,
            "BINDING_RECONCILED",
            {"binding_id": binding_id, "binding_status": binding.get("status")},
        )

        provider_reachable = self._probe_provider(executor_id)
        _emit_recovery_event(
            self.project_root,
            goal_run_id,
            "PROVIDER_PROBED",
            {"executor_id": executor_id, "reachable": provider_reachable},
        )

        binding_status = str(binding.get("status") or "")
        # A binding that already reached a terminal state carries an explicit
        # outcome; never widen it back to running.
        if binding_status == str(BindingStatus.COMPLETED):
            return await self._finalize(
                state,
                RecoveryOutcome.RECOVERY_REQUIRED,
                reason="binding_completed_without_goal_terminal",
                binding_id=binding_id,
                executor_id=executor_id,
                provider_reachable=provider_reachable,
                binding_present=True,
            )
        if binding_status in (str(BindingStatus.FAILED), str(BindingStatus.LOST)):
            return await self._finalize(
                state,
                RecoveryOutcome.LOST,
                reason=f"binding_{binding_status}",
                binding_id=binding_id,
                executor_id=executor_id,
                provider_reachable=provider_reachable,
                binding_present=True,
            )

        if not provider_reachable:
            return await self._finalize(
                state,
                RecoveryOutcome.RECOVERY_REQUIRED,
                reason="provider_unavailable",
                binding_id=binding_id,
                executor_id=executor_id,
                provider_reachable=False,
                binding_present=True,
            )

        # Provider reachable and binding non-terminal: the run is resumable.
        # Recovery does NOT launch a second execution; it only relabels the
        # durable run so an operator/continuation can resume it explicitly.
        return await self._finalize(
            state,
            RecoveryOutcome.RESUMABLE,
            reason="provider_reachable_binding_live",
            binding_id=binding_id,
            executor_id=executor_id,
            provider_reachable=True,
            binding_present=True,
        )

    async def reconcile_all(self) -> list[RecoveryVerdict]:
        """Reconcile every discovered live run."""
        return [await self.reconcile(goal_run_id) for goal_run_id in self.discover()]

    # ── Resource recovery ───────────────────────────────────────────

    def recover_resources(self) -> dict[str, int]:
        """Restore scopes, restore active leases, reclaim orphan leases.

        Ordinary operations admitted before the restart are not re-approved:
        their ExecutionScope is restored from durable state.
        """
        scopes = restore_scopes(self.project_root)
        leases = self.admission.restore_leases(self.project_root)
        orphans = self.admission.reclaim_orphan_leases(self.project_root)
        _emit_recovery_event(
            self.project_root,
            "system",
            "LEASE_RECONCILED",
            {"scopes_restored": scopes, "leases_restored": leases, "orphans_reclaimed": orphans},
        )
        return {
            "scopes_restored": scopes,
            "leases_restored": leases,
            "orphans_reclaimed": orphans,
        }

    # ── Helpers ─────────────────────────────────────────────────────

    def _load_binding(self, binding_id: str) -> dict[str, Any] | None:
        for record in _load_binding_records(self.project_root):
            if record.get("binding_id") == binding_id:
                return record
        return None

    def _probe_provider(self, executor_id: str) -> bool:
        if not executor_id:
            return False
        try:
            from veya.remote.executor_registry import get_executor_registry

            identity = get_executor_registry().identity(executor_id)
        except Exception:
            return False
        availability = str(identity.availability)
        if availability in ("UNAVAILABLE", "DISABLED", "LEGACY"):
            return False
        return bool(identity.selectable)

    async def _finalize(
        self,
        state: GoalRunState,
        outcome: RecoveryOutcome,
        *,
        reason: str,
        binding_id: str | None,
        executor_id: str | None,
        provider_reachable: bool | None,
        binding_present: bool,
    ) -> RecoveryVerdict:
        goal_run_id = state.goal_id
        # Never complete. Live runs move to recovering; a genuinely terminal
        # run is left untouched.
        if state.status not in _TERMINAL_GOAL_STATUSES:
            state.status = GoalStatus.recovering
            state.last_stop_reason = f"recovery_{outcome.value.lower()}: {reason}"
            save_goal_run(state, self.project_root)

        event_type = {
            RecoveryOutcome.RESUMABLE: "RECOVERY_RESUMED",
            RecoveryOutcome.RUNNING: "RECOVERY_RESUMED",
            RecoveryOutcome.RECOVERY_REQUIRED: "RECOVERY_REQUIRED",
            RecoveryOutcome.LOST: "RECOVERY_FAILED",
            RecoveryOutcome.FAILED: "RECOVERY_FAILED",
        }[outcome]
        _emit_recovery_event(
            self.project_root,
            goal_run_id,
            event_type,
            {"outcome": outcome.value, "reason": reason, "binding_id": binding_id},
        )
        return RecoveryVerdict(
            goal_run_id=goal_run_id,
            outcome=outcome,
            binding_id=binding_id,
            executor_id=executor_id,
            reason=reason,
            provider_reachable=provider_reachable,
            binding_present=binding_present,
        )


def get_recovery(project_root: str) -> ExecutionRecovery:
    return ExecutionRecovery(project_root)


def recover_after_restart(project_root: str) -> dict[str, Any]:
    """Full startup recovery: restore resources then reconcile live runs.

    Synchronous wrapper so it can be called from a startup hook. It does not
    block on provider network calls beyond the registry probe, and it never
    launches an execution.
    """
    recovery = ExecutionRecovery(project_root)
    resources = recovery.recover_resources()
    verdicts: list[dict[str, Any]] = []

    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # Already inside an event loop: schedule and do not block startup.
        task = loop.create_task(recovery.reconcile_all())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        verdicts = []
    else:
        results = asyncio.run(recovery.reconcile_all())
        verdicts = [v.to_dict() for v in results]

    return {"resources": resources, "verdicts": verdicts}
