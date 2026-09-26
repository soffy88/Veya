"""Durable storage for P15 doctrine records.

This is a persistence projection only.  It never activates policy, changes
tool authority, or replaces MissionStore/GoalRun.  Activation remains an
explicit authorized operation outside this store.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, cast


class DoctrineStore:
    """Atomic JSON documents plus append-only records under a project root."""

    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root) / ".veya-project" / "doctrine"

    def _write(self, name: str, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{name}.json"
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _read(self, name: str) -> dict[str, Any] | None:
        path = self.root / f"{name}.json"
        if not path.is_file():
            return None
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))

    def save_profile(self, profile: Any) -> None:
        self._write(f"verification-profile-{profile.id}", _record(profile))

    def load_profile(self, profile_id: str) -> dict[str, Any] | None:
        return self._read(f"verification-profile-{profile_id}")

    def save_playbook(self, playbook: Any) -> None:
        self._write(f"playbook-{playbook.playbook_id}-{playbook.version}", _record(playbook))

    def load_playbook(self, playbook_id: str, version: str) -> dict[str, Any] | None:
        return self._read(f"playbook-{playbook_id}-{version}")

    def append_correction(self, correction: Any) -> None:
        self._append("corrections", correction)

    def append_rule_candidate(self, candidate: Any) -> None:
        self._append("rule-candidates", candidate)

    def save_routine_state(self, routine_id: str, state: dict[str, Any]) -> None:
        self._write(f"routine-{routine_id}", dict(state))

    def load_routine_state(self, routine_id: str) -> dict[str, Any] | None:
        return self._read(f"routine-{routine_id}")

    def _append(self, name: str, value: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / f"{name}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_record(value), ensure_ascii=False) + "\n")

    def list_records(self, name: str) -> list[dict[str, Any]]:
        path = self.root / f"{name}.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def _record(value: Any) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    raise TypeError(f"doctrine value must be dataclass or dict, got {type(value).__name__}")


__all__ = ["DoctrineStore"]
