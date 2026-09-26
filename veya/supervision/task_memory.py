"""Durable Task Working Memory — a compact projection for the LLM (plane A).

This is **not** a Mission authority. ``MISSION_STORE_AUTHORITY=YES`` /
``MARKDOWN_PLAN_AUTHORITY=NO``: the files under ``.veya/tasks/<mission_id>/`` are
a human/LLM-readable projection of the canonical Mission/Execution/Evidence state.
Nothing here may be read back as authoritative Mission state.

Layout::

    .veya/tasks/<mission_id>/
      plan.md  findings.md  progress.md
      errors.jsonl  decisions.jsonl  workers/<execution_id>.jsonl
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Files are projections only; the canonical MissionStore is the authority.
MISSION_STORE_AUTHORITY = True
MARKDOWN_PLAN_AUTHORITY = False


@dataclass
class Plan:
    goal: str
    constraints: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    current_phase: str = ""
    dependencies: str = ""
    open_blockers: list[str] = field(default_factory=list)

    def compact(self) -> str:
        lines = [f"goal: {self.goal}"]
        if self.constraints:
            lines.append("constraints: " + "; ".join(self.constraints))
        if self.acceptance:
            lines.append("acceptance: " + "; ".join(self.acceptance))
        if self.phases:
            lines.append(f"phases: {' -> '.join(self.phases)}")
        lines.append(f"current_phase: {self.current_phase or 'unknown'}")
        if self.dependencies:
            lines.append(f"dependencies: {self.dependencies}")
        if self.open_blockers:
            lines.append("open_blockers: " + "; ".join(self.open_blockers))
        return "\n".join(lines)


class TaskMemory:
    """Projection + compact context recovery for one Mission."""

    def __init__(self, mission_root: str | Path, mission_id: str) -> None:
        self.mission_id = mission_id
        self.root = Path(mission_root).expanduser().resolve() / ".veya" / "tasks" / mission_id
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "workers").mkdir(exist_ok=True)

    # ── plan ────────────────────────────────────────────────────────
    def write_plan(self, plan: Plan) -> None:
        body = [f"# Plan — {self.mission_id}", "", plan.compact(), ""]
        (self.root / "plan.md").write_text("\n".join(body), encoding="utf-8")

    def read_plan(self) -> Plan | None:
        path = self.root / "plan.md"
        if not path.is_file():
            return None
        # Only the compact summary is re-injected; the markdown is a projection.
        return Plan(
            goal=path.read_text(encoding="utf-8").splitlines()[2:3][0].removeprefix("goal: ")
        )

    # ── findings / progress ─────────────────────────────────────────
    def add_finding(self, text: str, *, evidence: str) -> None:
        self._append(self.root / "findings.md", f"- {text} (evidence: {evidence})")

    def add_progress(self, milestone: str) -> None:
        self._append(self.root / "progress.md", f"- [{time.strftime('%H:%M:%S')}] {milestone}")

    # ── errors / repetition guard ───────────────────────────────────
    def record_error(
        self,
        *,
        action: str,
        error_class: str,
        attempt: int,
        evidence: str,
        resolution: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "action": action,
            "error_class": error_class,
            "attempt": attempt,
            "evidence": evidence,
            "resolution": resolution,
            "ts": time.time(),
        }
        self._append_jsonl(self.root / "errors.jsonl", entry)
        return entry

    def unresolved_errors(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for entry in self._read_jsonl(self.root / "errors.jsonl"):
            if entry.get("resolution") is None:
                out.append(entry)
        return out

    def should_skip_repeat(self, *, action: str, error_class: str) -> bool:
        """Same failure signature, latest attempt still unresolved -> do not repeat."""

        latest: dict[str, Any] | None = None
        for entry in self._read_jsonl(self.root / "errors.jsonl"):
            if entry.get("action") == action and entry.get("error_class") == error_class:
                latest = entry
        return latest is not None and latest.get("resolution") is None

    # ── decisions / workers ─────────────────────────────────────────
    def record_decision(self, *, decision: str, reason: str) -> None:
        self._append_jsonl(
            self.root / "decisions.jsonl",
            {"decision": decision, "reason": reason, "ts": time.time()},
        )

    def record_worker_event(self, execution_id: str, event: dict[str, Any]) -> None:
        self._append_jsonl(
            self.root / "workers" / f"{execution_id}.jsonl", {**event, "ts": time.time()}
        )

    # ── context recovery ────────────────────────────────────────────
    def recovery_context(self, *, recent: int = 5) -> dict[str, Any]:
        """Mission state + compact plan + relevant findings + recent progress + errors.

        Deliberately bounded: never the full history.
        """

        plan_text = ""
        plan_path = self.root / "plan.md"
        if plan_path.is_file():
            plan_text = plan_path.read_text(encoding="utf-8")
        findings = self._read_lines(self.root / "findings.md")[-recent:]
        progress = self._read_lines(self.root / "progress.md")[-recent:]
        return {
            "mission_id": self.mission_id,
            "mission_store_authority": MISSION_STORE_AUTHORITY,
            "markdown_plan_authority": MARKDOWN_PLAN_AUTHORITY,
            "compact_plan": plan_text,
            "recent_findings": findings,
            "recent_progress": progress,
            "unresolved_errors": self.unresolved_errors()[-recent:],
        }

    # ── internals ───────────────────────────────────────────────────
    @staticmethod
    def _append(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    @staticmethod
    def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        if not path.is_file():
            return []
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


__all__ = ["MARKDOWN_PLAN_AUTHORITY", "MISSION_STORE_AUTHORITY", "Plan", "TaskMemory"]
