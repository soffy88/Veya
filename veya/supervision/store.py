"""Mission persistence — orchestration metadata on the existing durable root.

Reuses the project's existing ``.veya-project`` durable root and the same
conventions as ``server/goal_run/store.py`` (atomic JSON + append-only JSONL
events). This is *not* a second checkpoint/artifact/worker store: execution
state stays authoritative in GoalRun / runtime execution. A mission document
only references ``goalrun_id`` / ``checkpoint_id`` / artifact refs / reviews.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .models import ExecutionReport, Mission, MissionStatus, SupervisorReview

_MISSIONS_DIR = ".veya-project/missions"
_MISSION_JSON = "mission.json"
_EVENTS_JSONL = "events.jsonl"
_REVIEWS_JSONL = "reviews.jsonl"
_REPORTS_JSONL = "reports.jsonl"
_EXECUTIONS_JSONL = "executions.jsonl"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class MissionStore:
    """Durable orchestration metadata for missions (spec §4/§19)."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root)

    # ── paths ───────────────────────────────────────────────────────
    def mission_dir(self, mission_id: str) -> Path:
        return self.project_root / _MISSIONS_DIR / mission_id

    def _ensure_dir(self, mission_id: str) -> Path:
        directory = self.mission_dir(mission_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    # ── mission document ────────────────────────────────────────────
    def save(self, mission: Mission) -> Mission:
        mission.touch()
        directory = self._ensure_dir(mission.mission_id)
        _atomic_write_json(directory / _MISSION_JSON, mission.to_dict())
        return mission

    def load(self, mission_id: str) -> Mission | None:
        path = self.mission_dir(mission_id) / _MISSION_JSON
        if not path.is_file():
            return None
        return Mission.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> list[Mission]:
        root = self.project_root / _MISSIONS_DIR
        if not root.is_dir():
            return []
        missions: list[Mission] = []
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and (entry / _MISSION_JSON).is_file():
                missions.append(
                    Mission.from_dict(json.loads((entry / _MISSION_JSON).read_text("utf-8")))
                )
        return missions

    def set_status(self, mission_id: str, status: MissionStatus) -> Mission:
        mission = self.load(mission_id)
        if mission is None:
            raise KeyError(f"unknown mission: {mission_id}")
        mission.status = status
        return self.save(mission)

    def set_supervision_mode(self, mission_id: str, mode: str) -> Mission:
        """Change ownership of *future* reviews only; never rebuilds the mission."""

        mission = self.load(mission_id)
        if mission is None:
            raise KeyError(f"unknown mission: {mission_id}")
        from .models import SupervisionMode

        mission.supervision_mode = SupervisionMode(mode)
        return self.save(mission)

    # ── linkage to the execution authority ──────────────────────────
    def link_goalrun(
        self,
        mission_id: str,
        goalrun_id: str,
        checkpoint_id: str | None = None,
    ) -> Mission:
        mission = self.load(mission_id)
        if mission is None:
            raise KeyError(f"unknown mission: {mission_id}")
        mission.authority["goalrun_id"] = goalrun_id
        if checkpoint_id is not None:
            mission.authority["checkpoint_id"] = checkpoint_id
        return self.save(mission)

    # ── append-only logs ────────────────────────────────────────────
    def append_event(
        self, mission_id: str, topic: str, payload: dict[str, Any] | None = None
    ) -> None:
        directory = self._ensure_dir(mission_id)
        record = {"ts": time.time(), "topic": topic, **(payload or {})}
        with (directory / _EVENTS_JSONL).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def events(self, mission_id: str) -> list[dict[str, Any]]:
        path = self.mission_dir(mission_id) / _EVENTS_JSONL
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def append_review(self, review: SupervisorReview) -> None:
        directory = self._ensure_dir(review.mission_id)
        with (directory / _REVIEWS_JSONL).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(review.to_dict(), ensure_ascii=False) + "\n")

    def reviews(self, mission_id: str) -> list[SupervisorReview]:
        path = self.mission_dir(mission_id) / _REVIEWS_JSONL
        if not path.is_file():
            return []
        return [
            SupervisorReview.from_dict(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def latest_review(self, mission_id: str) -> SupervisorReview | None:
        reviews = self.reviews(mission_id)
        return reviews[-1] if reviews else None

    # ── ExecutionReport log (evidence, not a second artifact store) ──
    def append_report(self, report: ExecutionReport) -> None:
        directory = self._ensure_dir(report.mission_id)
        with (directory / _REPORTS_JSONL).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report.to_dict(), ensure_ascii=False) + "\n")
        self.append_event(
            report.mission_id,
            "EVIDENCE_COLLECTED",
            {"iteration": report.iteration, "status": report.status},
        )

    def reports(self, mission_id: str) -> list[ExecutionReport]:
        path = self.mission_dir(mission_id) / _REPORTS_JSONL
        if not path.is_file():
            return []
        return [
            ExecutionReport.from_dict(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def latest_report(self, mission_id: str) -> ExecutionReport | None:
        reports = self.reports(mission_id)
        return reports[-1] if reports else None

    def get_report(self, mission_id: str, iteration: int) -> ExecutionReport | None:
        # last match wins: the loop re-appends a report when it attaches Jev
        # decisions, so the most recent line for the iteration is authoritative.
        for report in reversed(self.reports(mission_id)):
            if report.iteration == iteration:
                return report
        return None

    # ── execution handles (durable job reconciliation) ──────────────
    # Reuses this same mission store — no separate job store. The handle is what
    # lets a restarted process reconcile an in-flight execution instead of
    # blindly re-dispatching work that may already have had side effects.
    def append_execution(self, mission_id: str, payload: dict[str, Any]) -> None:
        directory = self._ensure_dir(mission_id)
        record = dict(payload)
        record.setdefault("at", time.time())
        with (directory / _EXECUTIONS_JSONL).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def executions(self, mission_id: str) -> list[dict[str, Any]]:
        path = self.mission_dir(mission_id) / _EXECUTIONS_JSONL
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def execution_for(self, mission_id: str, iteration: int) -> dict[str, Any] | None:
        """Authoritative handle for one iteration (last line for that execution_id)."""
        latest: dict[str, Any] = {}
        for record in self.executions(mission_id):
            if int(record.get("iteration", -1)) != int(iteration):
                continue
            key = str(record.get("execution_id", ""))
            latest[key] = {**latest.get(key, {}), **record}
        if not latest:
            return None
        return max(latest.values(), key=lambda item: float(item.get("at", 0.0)))

    def authority_for_execution(self, mission_id: str, iteration: int) -> dict[str, str]:
        """Project real execution_id and goalrun_id from the durable handle into mission authority.

        This is the canonical source that the frontend reads via the HTTP API. It
        never invents IDs; it only maps what the execution loop already persisted.
        """
        handle = self.execution_for(mission_id, iteration)
        if handle is None:
            return {}
        return {
            "execution_id": str(handle.get("execution_id", "")),
            "goalrun_id": str(handle.get("goalrun_id", "")),
            "iteration": str(iteration),
        }

    def link_execution_authority(self, mission_id: str, iteration: int) -> Mission:
        """Copy durable execution_id/goalrun_id/iteration into mission.authority.

        Canonical source of truth for the frontend via HTTP inspect. This is
        a projection only: no new ID is created and no execution side effect
        happens.
        """
        authority = self.authority_for_execution(mission_id, iteration)
        mission = self.load(mission_id)
        if mission is None:
            raise KeyError(f"unknown mission: {mission_id}")
        for key, value in authority.items():
            mission.authority[key] = value
        return self.save(mission)
