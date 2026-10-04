"""Durable failover ledger: the memory executor reselection was missing.

Executor failover already existed (``veya/supervision/runner.py``), but the
audit found its only two memory mechanisms are process-local:

* ``attempted`` is rebuilt as a fresh single-element set each iteration
  (``runner.py:351``), so it is not history;
* ``ExecutorHealthRegistry`` holds only ``self._records`` with no persistence
  (``executor_health.py:324``), and a fresh record reads as HEALTHY
  (``executor_health.py:440``).

So a restart forgets which executor failed, and two supervisors in different
processes each hold an independent view and can admit a different executor for
one task. This module supplies the durable record that makes the spec's §11
idempotency and §12 single-active-admission achievable, and it does so without
inventing a second locking authority: exclusion is a lock file created with
``O_CREAT | O_EXCL``, which is atomic on the same filesystem, and every mutation
is a read-modify-write of one revisioned file.

Two rules the rest of the design leans on:

* An idempotency key is ``mission + iteration + task + failure_event``. A repeat
  request with the same key returns the stored receipt instead of opening a
  second attempt — that is §11, and it is what stops an unbounded attempt loop.
* One task has at most one *active* admission. A second request for the same
  goal_run and task is refused, which is §12. A different failure event for a
  task that already holds an active admission is refused too; that is the
  concurrent-supervisor case and it must not silently stack attempts.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ACTIVE",
    "AdmissionOutcome",
    "FailoverLedger",
    "admit",
    "ledger_path",
    "read_ledger",
]

#: An admitted failover is active until the execution it authorised is no longer
#: running. Nothing closes it automatically: only the caller that owns the
#: execution knows that, so a stale ACTIVE entry is visible rather than guessed.
ACTIVE = "ACTIVE"

_TERMINAL = frozenset({"COMPLETED", "FAILED", "ABANDONED"})

LOCK_SUFFIX = ".lock"
LEDGER_SUFFIX = ".failover.json"


def ledger_path(project_root: str | Path, goal_run_id: str) -> Path:
    """``<project>/.veya/runs/<goal_run_id>.failover.json``."""
    return (
        Path(project_root).expanduser().resolve()
        / ".veya"
        / "runs"
        / f"{goal_run_id}{LEDGER_SUFFIX}"
    )


@dataclass
class AdmissionOutcome:
    """What the ledger decided, and why."""

    accepted: bool
    reason: str
    receipt: dict[str, Any] | None = None
    replayed: bool = False

    def with_holder(self, held: dict[str, Any]) -> AdmissionOutcome:
        """Attach the conflicting entry so a refusal is explainable.

        §10 asks a receipt to answer "why was another candidate not chosen". A
        refusal that names the holder answers the same question for the
        admission it prevented.
        """
        payload = dict(self.receipt or {})
        payload["conflicting_admission"] = {
            "idempotency_key": held.get("idempotency_key"),
            "failure_event_id": held.get("failure_event_id"),
            "admitted_at": held.get("admitted_at"),
            "selected_executor": (held.get("receipt") or {}).get("selected_executor"),
        }
        self.receipt = payload
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "receipt": self.receipt,
            "replayed": self.replayed,
        }


@dataclass
class FailoverLedger:
    """Revisioned, lock-guarded failover admissions for one goal run."""

    path: Path

    @classmethod
    def for_goal_run(cls, project_root: str | Path, goal_run_id: str) -> FailoverLedger:
        return cls(ledger_path(project_root, goal_run_id))

    # ── locking ────────────────────────────────────────────────────────
    @contextmanager
    def _exclusive(self, *, timeout_s: float = 10.0, poll_s: float = 0.02):
        """Mutual exclusion across processes.

        ``O_CREAT | O_EXCL`` is the only filesystem primitive guaranteed atomic
        for file creation, so it is what makes this a real exclusion rather than
        an in-process illusion. The lock is removed in a finally block; a stale
        lock from a killed process is reported rather than broken, because
        breaking another process's lock silently is how two writers end up
        believing they both won.
        """
        lock = self.path.with_name(self.path.name + LOCK_SUFFIX)
        lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + timeout_s
        fd: int | None = None
        while fd is None:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if time.time() >= deadline:
                    raise TimeoutError(
                        f"failover ledger lock held for {timeout_s}s: {lock}"
                    ) from None
                time.sleep(poll_s)
        try:
            os.write(fd, str(os.getpid()).encode("utf-8"))
            os.close(fd)
            fd = None
            yield
        finally:
            if fd is not None:
                os.close(fd)
            lock.unlink(missing_ok=True)

    # ── read / write ───────────────────────────────────────────────────
    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"revision": 0, "entries": []}
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            return {"revision": 0, "entries": []}
        data.setdefault("revision", 0)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data["revision"] = int(data.get("revision", 0)) + 1
        data["updated_at"] = time.time()
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    # ── the decision ───────────────────────────────────────────────────
    def admit(
        self,
        *,
        mission_id: str,
        iteration_id: str,
        task_id: str,
        failure_event_id: str,
        receipt: dict[str, Any],
    ) -> AdmissionOutcome:
        """Record one failover admission, or refuse it, idempotently.

        Order matters: the idempotency key is checked before the active-admission
        check, so a genuine retry of the *same* request replays its receipt
        instead of being refused as a duplicate.
        """
        key = _idempotency_key(mission_id, iteration_id, task_id, failure_event_id)
        with self._exclusive():
            data = self._read()
            entries: list[dict[str, Any]] = data["entries"]

            for entry in entries:
                if entry.get("idempotency_key") == key:
                    # Same request seen before. Replaying the stored receipt is
                    # what stops a retry loop from opening attempts forever.
                    return AdmissionOutcome(
                        accepted=True,
                        reason="IDEMPOTENT_REPLAY",
                        receipt=dict(entry.get("receipt") or {}),
                        replayed=True,
                    )

            active_for_task = [
                entry
                for entry in entries
                if entry.get("task_id") == task_id
                and entry.get("state") == ACTIVE
                and not _is_terminal(entry)
            ]
            if active_for_task:
                held = active_for_task[0]
                return AdmissionOutcome(
                    accepted=False,
                    reason="FAILOVER_ADMISSION_ALREADY_ACTIVE",
                    receipt=None,
                ).with_holder(held)

            stored = {
                "idempotency_key": key,
                "mission_id": mission_id,
                "iteration_id": iteration_id,
                "task_id": task_id,
                "failure_event_id": failure_event_id,
                "state": ACTIVE,
                "receipt": dict(receipt),
                "admitted_at": time.time(),
            }
            entries.append(stored)
            self._write(data)
            return AdmissionOutcome(
                accepted=True,
                reason="ADMITTED",
                receipt=dict(receipt),
                replayed=False,
            )

    def close(
        self,
        *,
        task_id: str,
        state: str,
        failure_event_id: str = "",
        detail: str = "",
    ) -> dict[str, Any] | None:
        """Move the active admission for a task to a terminal state.

        Called by whoever owns the execution. Absent on purpose: an entry left
        ACTIVE is a visible, recoverable condition, whereas a guessed terminal
        state is not.
        """
        if state not in _TERMINAL:
            raise ValueError(f"not a terminal failover state: {state!r}")
        with self._exclusive():
            data = self._read()
            changed = None
            for entry in data["entries"]:
                if entry.get("task_id") != task_id or entry.get("state") != ACTIVE:
                    continue
                if failure_event_id and entry.get("failure_event_id") != failure_event_id:
                    continue
                entry["state"] = state
                entry["closed_at"] = time.time()
                if detail:
                    entry["close_detail"] = detail
                changed = entry
            if changed is None:
                return None
            self._write(data)
            return dict(changed)

    def entries(self) -> list[dict[str, Any]]:
        return list(self._read()["entries"])

    def active_for(self, task_id: str) -> list[dict[str, Any]]:
        return [
            entry
            for entry in self._read()["entries"]
            if entry.get("task_id") == task_id and entry.get("state") == ACTIVE
        ]


def _is_terminal(entry: dict[str, Any]) -> bool:
    return str(entry.get("state", "")).upper() in _TERMINAL


def _idempotency_key(
    mission_id: str, iteration_id: str, task_id: str, failure_event_id: str
) -> str:
    """§11 key: mission + iteration + task + failure event.

    Dotted rather than hashed so the key is readable in the ledger file when a
    human is auditing why a second supervisor was refused.
    """
    return ".".join(
        str(part).strip() or "-" for part in (mission_id, iteration_id, task_id, failure_event_id)
    )


def read_ledger(project_root: str | Path, goal_run_id: str) -> list[dict[str, Any]]:
    return FailoverLedger.for_goal_run(project_root, goal_run_id).entries()


def admit(
    project_root: str | Path,
    goal_run_id: str,
    *,
    mission_id: str,
    iteration_id: str,
    task_id: str,
    failure_event_id: str,
    receipt: dict[str, Any],
) -> AdmissionOutcome:
    """Module-level convenience wrapper around :class:`FailoverLedger`."""
    return FailoverLedger.for_goal_run(project_root, goal_run_id).admit(
        mission_id=mission_id,
        iteration_id=iteration_id,
        task_id=task_id,
        failure_event_id=failure_event_id,
        receipt=receipt,
    )
