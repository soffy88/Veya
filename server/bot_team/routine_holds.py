"""Routine holds: a routine run waiting on the owner (OpenMausBot method, adapted).

A hold is the routine side of an attention item: the run stops, the owner
is asked through the attention queue, and the hold resolves when the
owner answers or dismisses. Holds are explicit records, never inferred
from routine state; resolving one does not re-trigger the routine, the
next scheduled run proceeds normally.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from server.bot_team.conversations import atomic_write_json, default_bots_root

HOLDS_FILENAME = "routine_holds.json"

HELD = "held"
RESUMED = "resumed"
DISMISSED = "dismissed"


class RoutineHoldStore:
    """Held/resumed/dismissed routine runs, one JSON file under bots root."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_bots_root()
        self.path = self.root / HOLDS_FILENAME

    def _load(self) -> list[dict[str, Any]]:
        try:
            import json

            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save(self, items: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, items)

    def create(
        self,
        *,
        bot_id: str,
        routine_id: str,
        reason: str,
        run_id: str | None = None,
        attention_id: str | None = None,
    ) -> dict[str, Any]:
        hold = {
            "id": f"hld_{uuid.uuid4().hex[:16]}",
            "bot_id": bot_id,
            "routine_id": routine_id,
            "run_id": run_id,
            "attention_id": attention_id,
            "reason": reason,
            "status": HELD,
            "created_ts": time.time(),
            "resolved_ts": None,
            "resolution_note": None,
        }
        items = self._load()
        items.append(hold)
        self._save(items)
        return hold

    def list(
        self,
        *,
        bot_id: str | None = None,
        routine_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        items = self._load()
        if bot_id is not None:
            items = [item for item in items if item.get("bot_id") == bot_id]
        if routine_id is not None:
            items = [item for item in items if item.get("routine_id") == routine_id]
        if status is not None:
            items = [item for item in items if item.get("status") == status]
        return items

    def get(self, hold_id: str) -> dict[str, Any] | None:
        for item in self._load():
            if item.get("id") == hold_id:
                return item
        return None

    def resolve(
        self, hold_id: str, *, status: str, note: str | None = None
    ) -> dict[str, Any] | None:
        items = self._load()
        for item in items:
            if item.get("id") == hold_id:
                if item.get("status") != HELD:
                    return None
                item["status"] = status
                item["resolved_ts"] = time.time()
                if note is not None:
                    item["resolution_note"] = note
                self._save(items)
                return item
        return None

    def resume_holds_for_attention(
        self, attention_id: str, *, outcome: str
    ) -> list[dict[str, Any]]:
        """Resolve every held hold linked to one attention item.

        ``outcome`` is "answered" (owner replied) or "dismissed" (owner
        closed the question without answering). Returns the holds resolved.
        """

        status = RESUMED if outcome == "answered" else DISMISSED
        resolved = []
        for item in self._load():
            if item.get("attention_id") == attention_id and item.get("status") == HELD:
                done = self.resolve(item["id"], status=status)
                if done is not None:
                    resolved.append(done)
        return resolved


__all__ = ["DISMISSED", "HELD", "RESUMED", "RoutineHoldStore"]
