"""Canonical resume-disposition policy (D3, Veya project execution policy).

ONE authority for execution-continuity decisions: given a trigger plus
evidence, decide deterministically among RESUME_SESSION / RECOVER_CHECKPOINT /
FRESH_TRANSIENT / FRESH_PERMANENT / FRESH_USER_RETRY / BLOCKED.

Boundary (D2 harness reports facts, D3 decides):
- harness adapters report ``session_id``, resume capability,
  ``HarnessErrorKind.RESUME_REJECTED`` and ``native_metadata`` — they never
  decide fresh/permanent/user-retry/checkpoint-recovery;
- this module decides the continuity action and records it auditably.

The module is stdlib-only and dependency-free on purpose: production call
sites inject their event emitter, so ``runtime/`` never imports ``server/``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Container, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "RESUME_DECIDED_TOPIC",
    "ResumeDecision",
    "ResumeDecisionStore",
    "ResumeDisposition",
    "ResumeTrigger",
    "decide_resume_disposition",
    "record_resume_decision",
    "rejection_evidence_from_harness",
    "verify_execution_checkpoint",
]

#: Canonical audit topic for resume/retry decisions (existing event model).
RESUME_DECIDED_TOPIC = "execution.resume_decided"

#: Schema version stamped on checkpoints this policy can verify.
CHECKPOINT_SCHEMA_VERSION = 1

_PERMANENT_MARKERS = (
    "not found",
    "expired",
    "invalid session",
    "invalid_session",
    "unsupported",
    "no such session",
    "unknown session",
)
_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "temporar",
    "busy",
    "locked",
    "rate limit",
    "429",
    "unavailable",
    "connection",
    "network",
)


class ResumeDisposition(StrEnum):
    """The six canonical continuity dispositions (single authority)."""

    RESUME_SESSION = "resume_session"
    RECOVER_CHECKPOINT = "recover_checkpoint"
    FRESH_TRANSIENT = "fresh_transient"
    FRESH_PERMANENT = "fresh_permanent"
    FRESH_USER_RETRY = "fresh_user_retry"
    BLOCKED = "blocked"


class ResumeTrigger(StrEnum):
    """What demanded a continuity decision (fail-closed on unknown)."""

    INFRA_RETRY = "infra_retry"
    PROCESS_RECOVERY = "process_recovery"
    USER_RERUN = "user_rerun"
    RESUME_REJECTED = "resume_rejected"
    MANUAL_RESUME = "manual_resume"

    @classmethod
    def coerce(cls, value: ResumeTrigger | str) -> ResumeTrigger:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        valid = sorted(member.value for member in cls)
        raise ValueError(f"unknown resume trigger {value!r}; expected one of {valid}")


@dataclass(frozen=True)
class ResumeDecision:
    """One deterministic continuity decision (value object, no behavior)."""

    disposition: ResumeDisposition
    reason: str
    trigger: ResumeTrigger
    session_id: str | None = None
    checkpoint_id: str | None = None
    retire_session: bool = False
    preserve_original_session: bool = True
    task_id: str | None = None
    run_id: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "disposition", _coerce_disposition(self.disposition))
        object.__setattr__(self, "trigger", ResumeTrigger.coerce(self.trigger))
        if not self.reason:
            raise ValueError("ResumeDecision.reason must not be empty")
        object.__setattr__(self, "evidence", dict(self.evidence))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def key(self) -> str:
        """Stable idempotency key: same evidence => same key."""
        canonical = json.dumps(
            {
                "trigger": self.trigger.value,
                "task_id": self.task_id,
                "run_id": self.run_id,
                "session_id": self.session_id,
                "checkpoint_id": self.checkpoint_id,
                "disposition": self.disposition.value,
                "retire_session": self.retire_session,
                "evidence": _canonical(self.evidence),
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]

    def to_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "reason": self.reason,
            "trigger": self.trigger.value,
            "session_id": self.session_id,
            "checkpoint_id": self.checkpoint_id,
            "retire_session": self.retire_session,
            "preserve_original_session": self.preserve_original_session,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "evidence": dict(self.evidence),
            "metadata": dict(self.metadata),
            "key": self.key(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ResumeDecision:
        raw = dict(data)
        raw.pop("key", None)
        return cls(**raw)


def _coerce_disposition(value: ResumeDisposition | str) -> ResumeDisposition:
    if isinstance(value, ResumeDisposition):
        return value
    if isinstance(value, str):
        try:
            return ResumeDisposition(value.strip().lower())
        except ValueError:
            pass
    valid = sorted(member.value for member in ResumeDisposition)
    raise ValueError(f"unknown resume disposition {value!r}; expected one of {valid}")


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def rejection_evidence_from_harness(
    error_kind: str | None, native_text: str | None = None
) -> str | None:
    """Map D2 harness rejection facts to policy evidence (facts in, no decision).

    Returns ``"permanent"`` / ``"transient"`` only on explicit markers,
    else ``None`` (unknown — the policy must then BLOCK, never guess).
    """
    text = f"{error_kind or ''} {(native_text or '')}".lower()
    if any(marker in text for marker in _PERMANENT_MARKERS):
        return "permanent"
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return "transient"
    return None


def verify_execution_checkpoint(
    checkpoint: Any, *, expected_lineage_id: str | None = None
) -> tuple[bool, str]:
    """Verify a canonical ExecutionCheckpoint for recovery (no guessing).

    Checks lineage, schema version, verified flag and structural integrity.
    Unverified or foreign checkpoints are never recovered — never because
    "a file exists".
    """
    if checkpoint is None:
        return False, "no-checkpoint"
    lineage = getattr(checkpoint, "lineage_id", None)
    snapshot = getattr(checkpoint, "scheduler_snapshot", None) or {}
    effective_lineage = lineage or (snapshot.get("goal_id") if isinstance(snapshot, dict) else None)
    if expected_lineage_id is not None and effective_lineage != expected_lineage_id:
        return False, "lineage-mismatch"
    if (
        getattr(checkpoint, "schema_version", CHECKPOINT_SCHEMA_VERSION)
        != CHECKPOINT_SCHEMA_VERSION
    ):
        return False, "version-mismatch"
    if not getattr(checkpoint, "verified", False):
        return False, "unverified"
    cursor = getattr(checkpoint, "event_cursor", None)
    if not isinstance(cursor, str) or not cursor:
        return False, "integrity-event-cursor"
    if not isinstance(snapshot, dict):
        return False, "integrity-scheduler-snapshot"
    return True, "verified"


def decide_resume_disposition(
    *,
    trigger: ResumeTrigger | str,
    task_id: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    checkpoint_id: str | None = None,
    checkpoint_verified: bool = False,
    checkpoint_lineage_match: bool = False,
    resume_capable: bool = False,
    rejection_evidence: str | None = None,
    prior_terminal_failed: bool = False,
    retired_sessions: Container[str] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ResumeDecision:
    """Decide execution continuity deterministically (pure function).

    Priority (fixed): user-rerun > process-recovery > infra-retry >
    resume-rejection(transient/permanent/unknown) > manual-resume, with a
    retired-session short-circuit for every non-user trigger. No timestamps
    or randomness touch the decision: same evidence => same disposition.
    """
    resolved_trigger = ResumeTrigger.coerce(trigger)
    retired = set(retired_sessions or ())
    evidence: dict[str, Any] = {
        "checkpoint_present": checkpoint_id is not None,
        "checkpoint_verified": bool(checkpoint_verified),
        "checkpoint_lineage_match": bool(checkpoint_lineage_match),
        "resume_capable": bool(resume_capable),
        "rejection_evidence": rejection_evidence,
        "prior_terminal_failed": bool(prior_terminal_failed),
        "session_retired": session_id in retired if session_id else False,
    }
    meta = dict(metadata or {})

    def _build(
        disposition: ResumeDisposition,
        reason: str,
        *,
        retire_session: bool = False,
    ) -> ResumeDecision:
        return ResumeDecision(
            disposition=disposition,
            reason=reason,
            trigger=resolved_trigger,
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            retire_session=retire_session,
            preserve_original_session=not retire_session,
            task_id=task_id,
            run_id=run_id,
            evidence=evidence,
            metadata=meta,
        )

    # A. Explicit human rerun after a bad result: always fresh reasoning.
    if resolved_trigger is ResumeTrigger.USER_RERUN:
        return _build(
            ResumeDisposition.FRESH_USER_RETRY,
            "explicit human rerun: fresh reasoning, old session audit-only",
        )

    # Retired sessions never resume again (stable, no duplicate retirement).
    if session_id and session_id in retired:
        return _build(
            ResumeDisposition.FRESH_PERMANENT,
            "session previously retired: fresh start, no duplicate retirement",
            retire_session=True,
        )

    # B. Process recovery: verified same-lineage checkpoint wins; a present
    # but unverifiable checkpoint blocks (never recover garbage/foreign).
    if resolved_trigger is ResumeTrigger.PROCESS_RECOVERY:
        if checkpoint_id is not None:
            if checkpoint_verified and checkpoint_lineage_match:
                return _build(
                    ResumeDisposition.RECOVER_CHECKPOINT,
                    "verified same-lineage checkpoint: recover",
                )
            return _build(
                ResumeDisposition.BLOCKED,
                "checkpoint present but unverified or foreign: refuse to recover",
            )
        if session_id and resume_capable:
            return _build(
                ResumeDisposition.RESUME_SESSION,
                "no checkpoint: continue resumable session",
            )
        return _build(
            ResumeDisposition.BLOCKED,
            "no checkpoint and no resumable session: insufficient evidence",
        )

    # C. Infrastructure retry: continuity first, never auto-fresh a live one.
    if resolved_trigger is ResumeTrigger.INFRA_RETRY:
        if session_id and resume_capable:
            return _build(
                ResumeDisposition.RESUME_SESSION,
                "infrastructure retry: keep continuity on resumable session",
            )
        if session_id:
            return _build(
                ResumeDisposition.BLOCKED,
                "infrastructure retry without resume capability: refuse silent fresh",
            )
        return _build(
            ResumeDisposition.FRESH_TRANSIENT,
            "infrastructure retry with nothing to resume: fresh attempt",
        )

    # D/E/F. Resume rejection: only real evidence moves off BLOCKED.
    if resolved_trigger is ResumeTrigger.RESUME_REJECTED:
        if rejection_evidence == "permanent":
            return _build(
                ResumeDisposition.FRESH_PERMANENT,
                "resume rejected with permanent evidence: fresh start, retire session",
                retire_session=session_id is not None,
            )
        if rejection_evidence == "transient":
            return _build(
                ResumeDisposition.FRESH_TRANSIENT,
                "resume rejected with transient evidence: fresh attempt, session preserved",
            )
        return _build(
            ResumeDisposition.BLOCKED,
            "resume rejected without permanence evidence: refuse to guess",
        )

    # Manual resume (human explicitly continues): the human's explicit
    # acceptance substitutes checkpoint verification, but lineage must
    # still match — a foreign lineage is never recovered, even for humans.
    if resolved_trigger is ResumeTrigger.MANUAL_RESUME:
        if checkpoint_id is not None:
            if checkpoint_lineage_match:
                return _build(
                    ResumeDisposition.RECOVER_CHECKPOINT,
                    "manual resume: human accepts task-scoped checkpoint",
                )
            return _build(
                ResumeDisposition.BLOCKED,
                "manual resume: checkpoint lineage mismatch",
            )
        if session_id and resume_capable:
            return _build(
                ResumeDisposition.RESUME_SESSION,
                "manual resume: human continues resumable session",
            )
        return _build(
            ResumeDisposition.BLOCKED,
            "manual resume without recoverable state: insufficient evidence",
        )

    raise AssertionError(f"unhandled resume trigger: {resolved_trigger!r}")


def record_resume_decision(
    decision: ResumeDecision, *, emit: Callable[[str, dict[str, Any]], Any]
) -> dict[str, Any]:
    """Audit one non-initial decision through the existing event model.

    ``emit`` is the caller's canonical emitter (e.g. a thin wrapper over
    ``server.events.append_canonical_event``); this module never imports it
    so ``runtime/`` keeps zero ``server/`` dependencies.
    """
    payload = decision.to_dict()
    return emit(RESUME_DECIDED_TOPIC, payload)


class ResumeDecisionStore:
    """Durable resume-decision records under one run root (decisions only).

    This stores decision records — never execution checkpoints — so it is
    not a second checkpoint store. Writes are atomic; ``record`` dedupes by
    decision key so duplicate evaluation never duplicates retirement or
    recovery side effects. Survives process restart.
    """

    FILENAME = "resume_decisions.json"

    def __init__(self, run_root: str | Path):
        self.run_root = Path(run_root).expanduser().resolve()
        self.path = self.run_root / "checkpoints" / self.FILENAME

    def _load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return {"decisions": [], "retired_sessions": []}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"decisions": [], "retired_sessions": []}
        if not isinstance(data, dict):
            return {"decisions": [], "retired_sessions": []}
        return {
            "decisions": list(data.get("decisions") or []),
            "retired_sessions": list(data.get("retired_sessions") or []),
        }

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def record(self, decision: ResumeDecision) -> dict[str, Any]:
        """Persist a decision idempotently; returns the stored record."""
        data = self._load()
        for existing in data["decisions"]:
            if existing.get("key") == decision.key():
                return existing
        stored = decision.to_dict()
        data["decisions"].append(stored)
        if (
            decision.retire_session
            and decision.session_id
            and decision.session_id not in data["retired_sessions"]
        ):
            data["retired_sessions"].append(decision.session_id)
        self._save(data)
        return stored

    def last(
        self, *, task_id: str | None = None, run_id: str | None = None
    ) -> dict[str, Any] | None:
        """Latest recorded decision, optionally scoped to one task/run."""
        matches = [
            item
            for item in self._load()["decisions"]
            if (task_id is None or item.get("task_id") == task_id)
            and (run_id is None or item.get("run_id") == run_id)
        ]
        return matches[-1] if matches else None

    def is_retired(self, session_id: str) -> bool:
        return session_id in self._load()["retired_sessions"]

    def retired_sessions(self) -> list[str]:
        return list(self._load()["retired_sessions"])
