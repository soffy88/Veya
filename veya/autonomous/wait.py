"""Wait Condition Management, External Dependencies, and Wake Contract (spec §17, §18, §19).

Invariants:
- Waits are durably persisted; no in-memory busy polling (BUSY_POLLING=0).
- Waking occurs via AgentRuntime trigger or event arrival; runtime only awakens.
- Upon wake, MasterAgent reassesses situation; runtime does not decide next step.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .models import (
    DependencyStatus,
    ExternalDependency,
    WaitCondition,
    WaitType,
)


class WaitConditionManager:
    """Manages durable wait conditions without busy polling (spec §17)."""

    def __init__(self, persistence_path: str | Path | None = None):
        self._path = Path(persistence_path) if persistence_path else None
        self._conditions: dict[str, WaitCondition] = {}
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
                    cond = WaitCondition.from_dict(data)
                    self._conditions[cond.condition_id] = cond
                except Exception:
                    continue

    def _persist(self, cond: WaitCondition) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(cond.to_dict(), ensure_ascii=False) + "\n")

    def register_wait(
        self,
        mission_id: str,
        wait_type: WaitType,
        predicate: str,
        timeout_s: float | None = None,
        wake_policy: str = "IMMEDIATE",
    ) -> WaitCondition:
        now = time.time()
        expires = (now + timeout_s) if timeout_s is not None else None
        cond = WaitCondition(
            mission_id=mission_id,
            condition_type=wait_type,
            predicate=predicate,
            expires_at=expires,
            wake_policy=wake_policy,
            status="PENDING",
            created_at=now,
        )
        self._conditions[cond.condition_id] = cond
        self._persist(cond)
        return cond

    def get_wait(self, condition_id: str) -> WaitCondition | None:
        return self._conditions.get(condition_id)

    def list_active_waits(self, mission_id: str | None = None) -> list[WaitCondition]:
        res = [c for c in self._conditions.values() if c.status == "PENDING"]
        if mission_id:
            res = [c for c in res if c.mission_id == mission_id]
        return res

    def evaluate_wake(self, condition_id: str, event_payload: dict[str, Any] | None = None) -> bool:
        cond = self._conditions.get(condition_id)
        if not cond or cond.status != "PENDING":
            return False

        now = time.time()
        # Check timeout expiration
        if cond.expires_at is not None and now >= cond.expires_at:
            cond.status = "EXPIRED"
            return True

        # Check predicate or event match
        if event_payload:
            topic = str(event_payload.get("topic") or event_payload.get("event") or "")
            if cond.predicate in topic or not cond.predicate:
                cond.status = "SATISFIED"
                return True

        return False

    def mark_satisfied(self, condition_id: str) -> None:
        cond = self._conditions.get(condition_id)
        if cond:
            cond.status = "SATISFIED"

    def resolve_wait(self, condition_id: str) -> None:
        self.mark_satisfied(condition_id)

    def check_all_satisfied(self) -> dict[str, list[tuple[str, bool]]]:
        res: dict[str, list[tuple[str, bool]]] = {}
        for cond in self._conditions.values():
            res.setdefault(cond.mission_id, []).append(
                (cond.condition_id, cond.status == "SATISFIED")
            )
        return res


class ExternalDependencyTracker:
    """Tracks external state dependencies (approvals, deployments, replies) (spec §19)."""

    def __init__(self):
        self._dependencies: dict[str, ExternalDependency] = {}

    def track(
        self,
        mission_id: str,
        kind: str,
        target: str,
        expected_state: str = "SATISFIED",
        check_interval_s: float = 60.0,
    ) -> ExternalDependency:
        dep = ExternalDependency(
            mission_id=mission_id,
            kind=kind,
            target=target,
            expected_state=expected_state,
            current_state="PENDING",
            last_checked_at=time.time(),
            next_check_at=time.time() + check_interval_s,
            status=DependencyStatus.PENDING,
        )
        self._dependencies[dep.dependency_id] = dep
        return dep

    def update_state(self, dependency_id: str, current_state: str) -> ExternalDependency | None:
        dep = self._dependencies.get(dependency_id)
        if not dep:
            return None
        dep.current_state = current_state
        dep.last_checked_at = time.time()
        if current_state == dep.expected_state:
            dep.status = DependencyStatus.SATISFIED
        return dep

    def get(self, dependency_id: str) -> ExternalDependency | None:
        return self._dependencies.get(dependency_id)

    def list_for_mission(self, mission_id: str) -> list[ExternalDependency]:
        return [d for d in self._dependencies.values() if d.mission_id == mission_id]
