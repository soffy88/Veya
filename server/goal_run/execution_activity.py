"""ExecutionActivity — unified execution evidence stream (P7).

This is an append-only evidence stream over the existing GoalRun event log.
It is NOT a durable authority: GoalRun remains the sole execution authority.
The activity stream exists so a client (or operator) can answer "what is
running, under which executor/provider, holding which leases, and what just
happened" and can resume a stream from a cursor after a disconnect.

Hard properties:

* one monotonic ``sequence`` per GoalRun, durable across restart;
* ``stream(cursor=N)`` returns only activities with ``sequence > N``;
* stdout/stderr is never stored unbounded: a bounded tail is kept and larger
  output is spilled to an artifact reference.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from server.goal_run.store import append_event

logger = logging.getLogger("veya.goal_run.activity")

_ACTIVITY_PREFIX = "execution_activity."
_ACTIVITY_DIR = ".veya-project/goal-runs"
#: Bounded tail for streamed output. Beyond this, output is spilled to an
#: artifact and only an artifact reference is kept in the activity payload.
OUTPUT_TAIL_BYTES = 32_768
_READ_LIMIT = 1 << 20


class ActivityType(StrEnum):
    """Canonical execution activity vocabulary (P7 §7.3)."""

    ADMISSION_CREATED = "ADMISSION_CREATED"
    ADMISSION_RELEASED = "ADMISSION_RELEASED"
    EXECUTOR_SELECTED = "EXECUTOR_SELECTED"
    EXECUTOR_HEALTH_CHANGED = "EXECUTOR_HEALTH_CHANGED"
    BINDING_CREATED = "BINDING_CREATED"
    BINDING_UPDATED = "BINDING_UPDATED"
    BINDING_LOST = "BINDING_LOST"
    PROCESS_STARTED = "PROCESS_STARTED"
    PROCESS_EXITED = "PROCESS_EXITED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    TOOL_FAILED = "TOOL_FAILED"
    OUTPUT = "OUTPUT"
    TEST_STARTED = "TEST_STARTED"
    TEST_COMPLETED = "TEST_COMPLETED"
    BUILD_STARTED = "BUILD_STARTED"
    BUILD_COMPLETED = "BUILD_COMPLETED"
    STEER_REQUESTED = "STEER_REQUESTED"
    STEER_COMPLETED = "STEER_COMPLETED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCEL_COMPLETED = "CANCEL_COMPLETED"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSE_COMPLETED = "PAUSE_COMPLETED"
    RESUME_REQUESTED = "RESUME_REQUESTED"
    RESUME_COMPLETED = "RESUME_COMPLETED"
    RECOVERY_STARTED = "RECOVERY_STARTED"
    RECOVERY_COMPLETED = "RECOVERY_COMPLETED"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    VERIFICATION_STARTED = "VERIFICATION_STARTED"
    VERIFICATION_COMPLETED = "VERIFICATION_COMPLETED"
    GOALRUN_COMPLETED = "GOALRUN_COMPLETED"
    GOALRUN_FAILED = "GOALRUN_FAILED"


@dataclass
class ExecutionActivity:
    """One execution activity event (P7 §7.2)."""

    activity_id: str
    goal_run_id: str
    sequence: int
    type: str
    phase: str
    payload: dict[str, Any]
    timestamp: float
    binding_id: str | None = None
    provider_id: str | None = None
    provider_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "activity_id": self.activity_id,
            "goal_run_id": self.goal_run_id,
            "sequence": self.sequence,
            "type": self.type,
            "phase": self.phase,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "binding_id": self.binding_id,
            "provider_id": self.provider_id,
            "provider_run_id": self.provider_run_id,
        }


@dataclass
class ObservabilitySnapshot:
    """Answer to 'what is happening right now' (P7 §7.6)."""

    running: list[dict[str, Any]] = field(default_factory=list)
    recent: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    recovery_required: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "recent": self.recent,
            "blocked": self.blocked,
            "recovery_required": self.recovery_required,
        }


class ActivityRecorder:
    """Append-only execution activity recorder over the GoalRun event log."""

    def __init__(self, project_root: str) -> None:
        self.project_root = os.path.realpath(project_root)
        self._max_seq: dict[str, int] = {}

    # ── sequence ────────────────────────────────────────────────────

    def _scan_max_sequence(self, goal_run_id: str) -> int:
        maximum = 0
        for event in _read_all_events(self.project_root, goal_run_id):
            if not str(event.get("type", "")).startswith(_ACTIVITY_PREFIX):
                continue
            try:
                maximum = max(maximum, int(event.get("sequence") or 0))
            except (TypeError, ValueError):
                continue
        return maximum

    def latest_sequence(self, goal_run_id: str) -> int:
        if goal_run_id not in self._max_seq:
            self._max_seq[goal_run_id] = self._scan_max_sequence(goal_run_id)
        return self._max_seq[goal_run_id]

    def _next_sequence(self, goal_run_id: str) -> int:
        seq = self.latest_sequence(goal_run_id) + 1
        self._max_seq[goal_run_id] = seq
        return seq

    # ── record ──────────────────────────────────────────────────────

    def record(
        self,
        goal_run_id: str,
        activity_type: ActivityType | str,
        phase: str,
        payload: dict[str, Any] | None = None,
        *,
        binding_id: str | None = None,
        provider_id: str | None = None,
        provider_run_id: str | None = None,
    ) -> ExecutionActivity:
        type_value = (
            activity_type.value if isinstance(activity_type, ActivityType) else str(activity_type)
        )
        seq = self._next_sequence(goal_run_id)
        activity = ExecutionActivity(
            activity_id=f"act_{goal_run_id}_{seq}_{uuid.uuid4().hex[:6]}",
            goal_run_id=goal_run_id,
            sequence=seq,
            type=type_value,
            phase=phase,
            payload=dict(payload or {}),
            timestamp=time.time(),
            binding_id=binding_id,
            provider_id=provider_id,
            provider_run_id=provider_run_id,
        )
        append_event(
            self.project_root,
            goal_run_id,
            {
                "type": f"{_ACTIVITY_PREFIX}{type_value}",
                "sequence": seq,
                "activity_id": activity.activity_id,
                "phase": phase,
                "payload": activity.payload,
                "binding_id": binding_id,
                "provider_id": provider_id,
                "provider_run_id": provider_run_id,
            },
        )
        return activity

    # ── stream ──────────────────────────────────────────────────────

    def stream(
        self, goal_run_id: str, cursor: int | None = None, limit: int = 500
    ) -> tuple[list[ExecutionActivity], int]:
        """Return activities with sequence > cursor, and the next cursor.

        The cursor is the last returned sequence; passing it back resumes
        exactly where the client left off.
        """
        start = cursor or 0
        activities: list[ExecutionActivity] = []
        for event in _read_all_events(self.project_root, goal_run_id):
            raw_type = str(event.get("type", ""))
            if not raw_type.startswith(_ACTIVITY_PREFIX):
                continue
            try:
                seq = int(event.get("sequence") or 0)
            except (TypeError, ValueError):
                continue
            if seq <= start:
                continue
            activities.append(
                ExecutionActivity(
                    activity_id=str(event.get("activity_id") or ""),
                    goal_run_id=goal_run_id,
                    sequence=seq,
                    type=raw_type[len(_ACTIVITY_PREFIX) :],
                    phase=str(event.get("phase") or ""),
                    payload=dict(event.get("payload") or {}),
                    timestamp=_coerce_timestamp(event.get("timestamp")),
                    binding_id=event.get("binding_id"),
                    provider_id=event.get("provider_id"),
                    provider_run_id=event.get("provider_run_id"),
                )
            )
        activities.sort(key=lambda a: a.sequence)
        if limit and len(activities) > limit:
            activities = activities[:limit]
        next_cursor = activities[-1].sequence if activities else start
        return activities, next_cursor

    # ── bounded output ──────────────────────────────────────────────

    def output(
        self,
        goal_run_id: str,
        stream_name: str,
        text: str,
        *,
        phase: str = "RUNNING",
    ) -> ExecutionActivity:
        """Record one bounded output chunk.

        Output larger than ``OUTPUT_TAIL_BYTES`` is spilled to an artifact and
        only an artifact reference is kept, so the stream never grows without
        bound.
        """
        encoded = text.encode("utf-8", "replace")
        if len(encoded) <= OUTPUT_TAIL_BYTES:
            return self.record(
                goal_run_id,
                ActivityType.OUTPUT,
                phase,
                {"stream": stream_name, "text": text},
            )
        artifact = self._spill_artifact(goal_run_id, stream_name, text)
        tail = text[-OUTPUT_TAIL_BYTES:]
        return self.record(
            goal_run_id,
            ActivityType.OUTPUT,
            phase,
            {
                "stream": stream_name,
                "tail": tail,
                "truncated": True,
                "artifact": artifact,
                "bytes": len(encoded),
            },
        )

    def _spill_artifact(self, goal_run_id: str, stream_name: str, text: str) -> str:
        artifacts_dir = Path(self.project_root) / _ACTIVITY_DIR / goal_run_id / "artifacts"
        try:
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            path = artifacts_dir / f"activity_output_{stream_name}_{uuid.uuid4().hex[:8]}.txt"
            path.write_text(text, encoding="utf-8", errors="replace")
            return str(path.relative_to(self.project_root))
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("failed to spill output artifact: %s", exc)
            return ""


# ── module-level convenience ────────────────────────────────────────


def get_recorder(project_root: str) -> ActivityRecorder:
    return ActivityRecorder(project_root)


def stream_activities(
    project_root: str, goal_run_id: str, cursor: int | None = None, limit: int = 500
) -> tuple[list[ExecutionActivity], int]:
    return ActivityRecorder(project_root).stream(goal_run_id, cursor=cursor, limit=limit)


def observability_snapshot(project_root: str) -> ObservabilitySnapshot:
    """Project the current execution control-plane state (P7 §7.6)."""
    from server.goal_run.execution_admission import get_admission
    from server.goal_run.models import GoalStatus
    from server.goal_run.store import load_goal_run

    root = os.path.realpath(project_root)
    admission = get_admission(root)
    recorder = ActivityRecorder(root)
    snapshot = ObservabilitySnapshot()

    # P7 §7.6: which resource leases does a run hold?
    leases_by_goal: dict[str, list[str]] = {}
    for lease in admission._leases.values():
        if lease.active():
            leases_by_goal.setdefault(lease.goal_run_id, []).append(str(lease.resource))

    runs_dir = Path(root) / _ACTIVITY_DIR
    if runs_dir.is_dir():
        for taskgraph in sorted(runs_dir.glob("*/taskgraph.json")):
            goal_id = taskgraph.parent.name
            state = load_goal_run(root, goal_id)
            if state is None:
                continue
            recent, _ = recorder.stream(
                goal_id, cursor=max(0, recorder.latest_sequence(goal_id) - 5)
            )
            entry = {
                "goal_run_id": goal_id,
                "status": state.status.value,
                "executor_id": state.requested_executor,
                "execution_id": state.execution_id,
                "workspace_root": state.runtime_checkpoint.get("workspace_root")
                if isinstance(state.runtime_checkpoint, dict)
                else None,
                "resource_leases": leases_by_goal.get(goal_id, []),
                "last_stop_reason": state.last_stop_reason,
                "recent": [a.type for a in recent],
            }
            if state.status == GoalStatus.running:
                snapshot.running.append(entry)
            elif state.status == GoalStatus.recovering:
                snapshot.recovery_required.append(entry)
            elif state.status in (GoalStatus.blocked, GoalStatus.failed):
                snapshot.blocked.append(entry)
            if recent:
                snapshot.recent.append(
                    {
                        "goal_run_id": goal_id,
                        "last_type": recent[-1].type,
                        "sequence": recent[-1].sequence,
                    }
                )
    snapshot.running.sort(key=lambda item: item["goal_run_id"])
    return snapshot


def _coerce_timestamp(value: Any) -> float:
    """Accept either an epoch float or an ISO-8601 string (GoalRun log format)."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    try:
        return float(text)
    except ValueError:
        pass
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _read_all_events(project_root: str, goal_run_id: str) -> list[dict[str, Any]]:
    path = Path(project_root) / _ACTIVITY_DIR / goal_run_id / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return events[-_READ_LIMIT:]
