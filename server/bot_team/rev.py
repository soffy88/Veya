"""Global revision counter, read marks, and unread counts (Codync method).

Every durable change -- a message appended, an attention item opened or
closed, a routine hold created or resolved -- bumps one global counter
stored as ``rev.json`` under the bots root, and every stored message is
stamped with the revision at which it landed. A client that polls
``GET /sync?since=N`` learns in one cheap read whether anything changed,
then re-fetches only the lists it shows; it never needs to poll full
transcripts to discover a change.

Per-room read marks (``read_marks.json``) record, per owner, the revision
up to which a room was read. The unread count for a room counts only
final answers (visible bot messages newer than the mark) plus pending
cards (open attention items, which count until resolved no matter when
they were created). Messages written before this graft carry no ``rev``
and read as revision 0 -- already read, never spuriously unread.

The counter is single-process by design: the read-modify-write runs as
synchronous file I/O inside the event loop with no await in between, so
two appends cannot interleave mid-bump. Gaps are fine; the counter only
ever moves forward.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from server.bot_team.conversations import atomic_write_json, default_bots_root

REV_FILE = "rev.json"
READ_MARKS_FILE = "read_marks.json"


def _base(root: str | Path | None) -> Path:
    return Path(root).expanduser() if root is not None else default_bots_root()


def current_rev(root: str | Path | None = None) -> int:
    """The current global revision; 0 when nothing has ever bumped it."""

    try:
        payload = json.loads((_base(root) / REV_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if isinstance(payload, dict) and isinstance(payload.get("rev"), int):
        return max(0, int(payload["rev"]))
    return 0


def bump_rev(root: str | Path | None = None) -> int:
    """Advance the global revision by one and return the new value."""

    base = _base(root)
    base.mkdir(parents=True, exist_ok=True)
    rev = current_rev(base) + 1
    atomic_write_json(base / REV_FILE, {"rev": rev})
    return rev


class ReadMarkStore:
    """Per-owner, per-room read revisions."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = _base(root)

    def _path(self) -> Path:
        return self.root / READ_MARKS_FILE

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def get(self, user_id: str, group_id: str) -> int:
        """The revision up to which *user_id* has read *group_id*; 0 if none."""

        marks = self._load().get(str(user_id))
        if not isinstance(marks, dict):
            return 0
        rev = marks.get(str(group_id))
        return rev if isinstance(rev, int) and rev > 0 else 0

    def mark(self, user_id: str, group_id: str, rev: int) -> int:
        """Record *rev* as read; never moves a mark backwards."""

        marks = self._load()
        per_user = marks.get(str(user_id))
        if not isinstance(per_user, dict):
            per_user = {}
            marks[str(user_id)] = per_user
        stored = max(self.get(user_id, group_id), max(0, int(rev)))
        per_user[str(group_id)] = stored
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._path(), marks)
        return stored


def unread_for_group(
    messages: list[dict[str, Any]], open_attention: int, read_rev: int
) -> int:
    """Unread count: final answers newer than the mark, plus pending cards."""

    fresh = sum(
        1
        for message in messages
        if message.get("role") == "bot"
        and not message.get("hidden")
        and int(message.get("rev") or 0) > read_rev
    )
    return fresh + max(0, int(open_attention))


__all__ = [
    "READ_MARKS_FILE",
    "REV_FILE",
    "ReadMarkStore",
    "bump_rev",
    "current_rev",
    "unread_for_group",
]
