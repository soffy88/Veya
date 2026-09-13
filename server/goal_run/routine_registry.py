"""Routine catalog: register/get/list for trigger-only routines (P2-C).

A routine describes *when* to start work (trigger topic) and *what* to hand
to MasterAgent (one skill, one playbook, or one goal template). The catalog
never executes, never finalizes, and never creates a GoalRun — dispatch lives
in :mod:`server.goal_run.routine_dispatch` and only writes trigger state into
the existing GoalRunState.

Kept inside the goal_run package (instead of capability_model) because
``server.goal_run.runner`` already imports ``server.capability_model``; the
reverse import would be circular.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from server.goal_run.models import ROUTINE_TRIGGER_TOPICS, RoutineSpec


def validate_routine_spec(spec: RoutineSpec) -> list[str]:
    """Validate a routine without executing anything (P2-C)."""
    errors: list[str] = []
    if not spec.routine_id:
        errors.append("routine_id is required")
    if spec.trigger_topic not in ROUTINE_TRIGGER_TOPICS:
        errors.append(
            f"unknown trigger_topic {spec.trigger_topic!r}; "
            f"expected one of {sorted(ROUTINE_TRIGGER_TOPICS)}"
        )
    targets = [
        bool(spec.target_skill_id),
        bool(spec.target_playbook_id),
        bool(spec.goal_template),
    ]
    if sum(targets) != 1:
        errors.append(
            "routine must declare exactly one target: "
            "target_skill_id, target_playbook_id, or goal_template"
        )
    if spec.version < 1:
        errors.append("version must be >= 1")
    return errors


class RoutineRegistry:
    """In-memory routine catalog with optional JSON file persistence."""

    def __init__(self, storage_path: str | Path | None = None):
        self.storage_path = (
            Path(storage_path)
            if storage_path is not None
            else Path(
                os.environ.get(
                    "VEYA_ROUTINE_REGISTRY_PATH",
                    str(Path.home() / ".veya" / "routine_registry.json"),
                )
            ).expanduser()
        )
        self._items: dict[str, RoutineSpec] = {}
        self._load()

    def _load(self) -> None:
        if not self.storage_path.exists():
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for routine_id, record in data.items():
            if isinstance(record, dict):
                self._items[str(routine_id)] = RoutineSpec.from_dict(record)

    def _save(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.storage_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {rid: spec.to_dict() for rid, spec in self._items.items()},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        os.replace(tmp, self.storage_path)

    def register(self, spec: RoutineSpec) -> None:
        errors = validate_routine_spec(spec)
        if errors:
            raise ValueError(f"invalid routine {spec.routine_id!r}: " + "; ".join(errors))
        self._items[spec.routine_id] = spec
        self._save()

    def get(self, routine_id: str, version: int | None = None) -> RoutineSpec | None:
        spec = self._items.get(routine_id)
        if spec is None:
            return None
        if version is not None and spec.version != version:
            return None
        return spec

    def list(self, *, include_disabled: bool = False) -> list[RoutineSpec]:
        items = list(self._items.values())
        if include_disabled:
            return items
        return [spec for spec in items if spec.enabled]

    def set_enabled(self, routine_id: str, enabled: bool) -> bool:
        spec = self._items.get(routine_id)
        if spec is None:
            return False
        spec.enabled = enabled
        self._save()
        return True

    def lookup(self, trigger_topic: str) -> list[RoutineSpec]:
        """Enabled routines for one trigger topic (structural match only)."""
        return [
            spec
            for spec in self._items.values()
            if spec.enabled and spec.trigger_topic == trigger_topic
        ]


__all__ = ["RoutineRegistry", "validate_routine_spec"]
