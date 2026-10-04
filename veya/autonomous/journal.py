"""Observation Journal and Context Reconciliation (spec §5, §24).

Invariants:
- Append-only observation store; never silently overwrite history.
- Provenance and freshness tracked.
- Context reconciliation marks CURRENT, STALE, CONFLICTING, UNVERIFIED.
- Unverified model outputs or stale observations are never treated as facts.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import Observation, ObservationSource, ObservationStatus


class ObservationJournal:
    """Durable append-only journal for observations (spec §5)."""

    def __init__(self, persistence_path: str | Path | None = None):
        self._path = Path(persistence_path) if persistence_path else None
        self._observations: list[Observation] = []
        self._by_id: dict[str, Observation] = {}
        self._by_dedup: dict[tuple[str, str], str] = {}  # (mission_id, dedup_key) -> obs_id
        #: Lines the journal could not decode, and the last decode error. An
        #: append-only log that silently drops unreadable records reads as a
        #: journal that never recorded them, so the damage is counted and kept
        #: rather than swallowed.
        self.unreadable_records = 0
        self.last_load_error: str | None = None
        if self._path and self._path.exists():
            self._load()

    def _load(self) -> None:
        if not self._path or not self._path.is_file():
            return
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    obs = Observation.from_dict(data)
                except Exception as exc:
                    self.unreadable_records += 1
                    if self.last_load_error is None:
                        self.last_load_error = f"{type(exc).__name__}: {exc}"
                    continue
                self._observations.append(obs)
                self._by_id[obs.observation_id] = obs
                if obs.dedup_key:
                    self._by_dedup[(obs.mission_id, obs.dedup_key)] = obs.observation_id

    def _persist(self, obs: Observation) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Opened per record and flushed, so a record that reaches the file is a
        # whole line. A long-lived handle left unflushed lost whatever the last
        # buffered writes were when the process died, which for an append-only
        # journal means the observation never existed.
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obs.to_dict(), ensure_ascii=False) + "\n")
            f.flush()

    def append(
        self,
        observation: Observation | None = None,
        *,
        mission_id: str = "default",
        source: ObservationSource = ObservationSource.SYSTEM,
        source_ref: str = "",
        kind: str = "general",
        summary: str = "",
        payload: dict[str, Any] | None = None,
        confidence: float = 1.0,
        status: ObservationStatus = ObservationStatus.CURRENT,
        dedup_key: str = "",
        supersedes: list[str] | None = None,
        contradicts: list[str] | None = None,
        confirms: list[str] | None = None,
    ) -> Observation:
        """Append an observation to the journal with deduplication and relationship tracking."""
        if observation is None:
            observation = Observation(
                mission_id=mission_id,
                source=source,
                source_ref=source_ref,
                kind=kind,
                summary=summary,
                details=dict(payload or {}),
                confidence=confidence,
                status=status,
                dedup_key=dedup_key,
                supersedes=list(supersedes or []),
                contradicts=list(contradicts or []),
                confirms=list(confirms or []),
            )
        # Check dedup
        if observation.dedup_key:
            existing_id = self._by_dedup.get((observation.mission_id, observation.dedup_key))
            if existing_id and existing_id in self._by_id:
                existing = self._by_id[existing_id]
                # Update freshness of existing
                existing.freshness = max(existing.freshness, observation.freshness)
                return existing

        # Apply relations
        for sup_id in observation.supersedes:
            if sup_id in self._by_id:
                self._by_id[sup_id].status = ObservationStatus.STALE

        for contra_id in observation.contradicts:
            if contra_id in self._by_id:
                self._by_id[contra_id].status = ObservationStatus.CONFLICTING
                observation.status = ObservationStatus.CONFLICTING

        for conf_id in observation.confirms:
            if (
                conf_id in self._by_id
                and self._by_id[conf_id].status == ObservationStatus.UNVERIFIED
            ):
                self._by_id[conf_id].status = ObservationStatus.CURRENT

        # Default unverified check for ungrounded tools
        if observation.confidence < 0.6:
            observation.status = ObservationStatus.UNVERIFIED

        self._observations.append(observation)
        self._by_id[observation.observation_id] = observation
        if observation.dedup_key:
            self._by_dedup[(observation.mission_id, observation.dedup_key)] = (
                observation.observation_id
            )
        self._persist(observation)
        return observation

    def query(
        self,
        mission_id: str,
        limit: int = 100,
        status: ObservationStatus | None = None,
        min_freshness: float | None = None,
    ) -> list[Observation]:
        """Query observations for a given mission."""
        res = [obs for obs in self._observations if obs.mission_id == mission_id]
        if status is not None:
            res = [obs for obs in res if obs.status == status]
        if min_freshness is not None:
            res = [obs for obs in res if obs.freshness >= min_freshness]
        return res[-limit:]

    def get(self, observation_id: str) -> Observation | None:
        return self._by_id.get(observation_id)

    def mark_stale(self, observation_id: str) -> None:
        obs = self._by_id.get(observation_id)
        if obs:
            obs.status = ObservationStatus.STALE

    def link_evidence(self, observation_id: str, evidence_ref: str) -> None:
        obs = self._by_id.get(observation_id)
        if obs:
            obs.payload_ref = evidence_ref
            if obs.status == ObservationStatus.UNVERIFIED:
                obs.status = ObservationStatus.CURRENT

    def count(self, mission_id: str | None = None) -> int:
        if mission_id is None:
            return len(self._observations)
        return sum(1 for obs in self._observations if obs.mission_id == mission_id)


class ReconciledContext(list):
    """Reconciled observations categorized by freshness and conflict status (spec §24)."""

    @property
    def current(self) -> list[Observation]:
        return [o for o in self if o.status == ObservationStatus.CURRENT]

    @property
    def stale(self) -> list[Observation]:
        return [o for o in self if o.status == ObservationStatus.STALE]

    @property
    def conflicting(self) -> list[Observation]:
        return [o for o in self if o.status == ObservationStatus.CONFLICTING]

    @property
    def unverified(self) -> list[Observation]:
        return [o for o in self if o.status == ObservationStatus.UNVERIFIED]


def reconcile_context(
    observations: Iterable[Observation],
    current_time: float | None = None,
    max_age_s: float = 3600.0,
) -> ReconciledContext:
    """Reconcile observations freshness, conflicts, and verification state (spec §24).

    Returns observations with updated statuses: CURRENT, STALE, CONFLICTING, UNVERIFIED.
    """
    now = current_time or time.time()
    obs_list = list(observations)
    obs_by_id = {obs.observation_id: obs for obs in obs_list}

    # Resolve cross-observation relations
    for obs in obs_list:
        for sup_id in obs.supersedes:
            if sup_id in obs_by_id:
                obs_by_id[sup_id].status = ObservationStatus.STALE

        for contra_id in obs.contradicts:
            if contra_id in obs_by_id:
                obs_by_id[contra_id].status = ObservationStatus.CONFLICTING
                obs.status = ObservationStatus.CONFLICTING

    reconciled: list[Observation] = []

    for obs in obs_list:
        # Check staleness based on wall-clock time
        if (now - obs.freshness) > max_age_s:
            obs.status = ObservationStatus.STALE
        elif obs.confidence < 0.6 and obs.status == ObservationStatus.CURRENT:
            obs.status = ObservationStatus.UNVERIFIED

        reconciled.append(obs)

    return ReconciledContext(reconciled)
