"""Attention queue (Soulgent method, adapted for veya).

Needs-you covers approvals: tool actions gated on the owner. This covers
the other half: questions a bot or a room asks the owner in conversation.
An item stays open until the owner answers it (delivered as a user
message to the bot conversation or the room transcript, so the bot sees
it on its next turn) or dismisses it.

Status derivation (used by the board and the inbox) reads only persisted
state, never guesses:
- ``waiting``: the bot/room has open attention items.
- ``working``: the last visible message is user-authored, so a reply is
  still owed (turn in flight, queued, or failed — all mean "not idle").
- ``idle``: anything else.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from server.bot_team.conversations import ConversationStore, atomic_write_json, default_bots_root
from server.bot_team.groups import GroupChat, GroupStore
from server.goal_run.bot_identity import BotRegistry, BotSpec

ATTENTION_FILENAME = "attention.json"

OPEN = "open"

#: What an attention item is waiting on. ``question`` is the
#: default; ``escalation`` is a room handing a stuck round to a human;
#: ``incident`` is a failed delegation or job reporting itself.
ATTENTION_KINDS = ("question", "escalation", "incident")
MAX_OPTIONS = 6
MAX_OPTION_CHARS = 200


def _validate_options(options: object) -> list[str] | None:
    """Normalise an options list, or refuse it whole (fail-closed).

    An option is one tappable answer on the card. Empty or over-long
    entries and more than MAX_OPTIONS are producer bugs: refuse the
    whole write instead of rendering a broken card.
    """

    if options is None:
        return None
    if not isinstance(options, list):
        raise ValueError("options must be a list of strings")
    cleaned: list[str] = []
    for entry in options:
        if not isinstance(entry, str):
            raise ValueError("options must be a list of strings")
        text = entry.strip()
        if not text:
            raise ValueError("options must not contain an empty option")
        if len(text) > MAX_OPTION_CHARS:
            raise ValueError(f"option exceeds {MAX_OPTION_CHARS} characters")
        if text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > MAX_OPTIONS:
        raise ValueError(f"at most {MAX_OPTIONS} options")
    return cleaned or None


ANSWERED = "answered"
DISMISSED = "dismissed"


class AttentionStore:
    """Open/dismissed/answered question items, one JSON file under bots root."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_bots_root()
        self.path = self.root / ATTENTION_FILENAME

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
        question: str,
        bot_id: str | None = None,
        group_id: str | None = None,
        context: str | None = None,
        kind: str = "question",
        options: list[str] | None = None,
    ) -> dict[str, Any]:
        if kind not in ATTENTION_KINDS:
            raise ValueError(f"unknown attention kind: {kind!r}")
        item = {
            "id": f"att_{uuid.uuid4().hex[:16]}",
            "bot_id": bot_id,
            "group_id": group_id,
            "question": question,
            "context": context,
            "kind": kind,
            "options": _validate_options(options),
            "status": OPEN,
            "created_ts": time.time(),
            "answered_ts": None,
            "answer": None,
        }
        items = self._load()
        items.append(item)
        self._save(items)
        from server.bot_team.rev import bump_rev

        bump_rev(root=self.root)
        return item

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        items = self._load()
        if status is not None:
            items = [item for item in items if item.get("status") == status]
        return items

    def get(self, item_id: str) -> dict[str, Any] | None:
        for item in self._load():
            if item.get("id") == item_id:
                return item
        return None

    def _close(self, item_id: str, *, status: str, answer: str | None = None) -> dict[str, Any] | None:
        items = self._load()
        for item in items:
            if item.get("id") == item_id:
                if item.get("status") != OPEN:
                    return None
                item["status"] = status
                item["answered_ts"] = time.time()
                if answer is not None:
                    item["answer"] = answer
                self._save(items)
                from server.bot_team.rev import bump_rev

                bump_rev(root=self.root)
                return item
        return None

    def answer(self, item_id: str, answer: str) -> dict[str, Any] | None:
        return self._close(item_id, status=ANSWERED, answer=answer)

    def dismiss(self, item_id: str) -> dict[str, Any] | None:
        return self._close(item_id, status=DISMISSED)

    def open_count(self, *, bot_id: str | None = None, group_id: str | None = None) -> int:
        count = 0
        for item in self._load():
            if item.get("status") != OPEN:
                continue
            if bot_id is not None and item.get("bot_id") != bot_id:
                continue
            if group_id is not None and item.get("group_id") != group_id:
                continue
            count += 1
        return count


def _status_from_messages(
    messages: list[dict[str, Any]], *, open_attention: int
) -> tuple[str, float | None]:
    if open_attention > 0:
        status = "waiting"
    elif messages and messages[-1].get("role") == "user":
        status = "working"
    else:
        status = "idle"
    last_ts = None
    if messages:
        raw_ts = messages[-1].get("ts")
        if isinstance(raw_ts, (int, float)):
            last_ts = float(raw_ts)
    return status, last_ts


def bot_board_entry(
    spec: BotSpec, *, conversations: ConversationStore, store: AttentionStore
) -> dict[str, Any]:
    messages = conversations.list(spec.bot_id)
    open_attention = store.open_count(bot_id=spec.bot_id)
    status, last_ts = _status_from_messages(messages, open_attention=open_attention)
    return {
        "bot_id": spec.bot_id,
        "name": spec.name or spec.bot_id,
        "status": status,
        "open_attention": open_attention,
        "last_active_ts": last_ts,
    }


def build_board(
    registry: BotRegistry,
    *,
    conversations: ConversationStore,
    store: AttentionStore,
    owner_id: str | None = None,
) -> list[dict[str, Any]]:
    from server.bot_team.chef import CHEF_BOT_ID
    from server.goal_run.bot_identity import DEFAULT_BOT_ID

    entries = []
    for spec in registry.list():
        if spec.bot_id not in (DEFAULT_BOT_ID, CHEF_BOT_ID):
            if owner_id is not None and spec.owner_id != owner_id:
                continue
        entries.append(bot_board_entry(spec, conversations=conversations, store=store))
    return entries


def build_inbox(
    registry: BotRegistry,
    *,
    conversations: ConversationStore,
    groups: GroupStore,
    store: AttentionStore,
    owner_id: str | None = None,
) -> dict[str, Any]:
    from server.bot_team.rev import ReadMarkStore, unread_for_group

    marks = ReadMarkStore(groups.root)
    group_rows = []
    for group in groups.list():
        messages = groups.messages(group.id)
        open_attention = store.open_count(group_id=group.id)
        status, last_ts = _status_from_messages(messages, open_attention=open_attention)
        read_rev = marks.get(owner_id or "", group.id)
        group_rows.append(
            {
                "group_id": group.id,
                "name": group.name,
                "reply_mode": group.reply_mode or "everyone",
                "member_count": len(group.bot_ids),
                "status": status,
                "open_attention": open_attention,
                "last_active_ts": last_ts,
                "read_rev": read_rev,
                "unread": unread_for_group(messages, open_attention, read_rev),
            }
        )
    open_items = store.list(status=OPEN)
    # Escalations wait on a human's decision: they pin above ordinary
    # questions, in creation order within each kind.
    open_items.sort(
        key=lambda item: (
            0 if item.get("kind") == "escalation" else 1,
            float(item.get("created_ts") or 0.0),
        )
    )
    return {
        "attention": open_items,
        "board": build_board(
            registry, conversations=conversations, store=store, owner_id=owner_id
        ),
        "groups": group_rows,
    }


__all__ = [
    "ANSWERED",
    "DISMISSED",
    "OPEN",
    "AttentionStore",
    "build_board",
    "build_inbox",
]
