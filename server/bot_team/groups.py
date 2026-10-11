"""Group chats between named bots (Lorca P2).

A group is a chat room with 1..6 member bots. One user message offers every
member one turn, in the order they were added, with the group transcript as
context. A member that has nothing to add answers with the PASS sentinel; that
reply is stored hidden and is never returned by the messages endpoint. When
every member passes, the round stops early instead of spending more turns.

Groups own no execution authority: each member turn is a normal product task
for that bot (see :mod:`server.bot_team.turns`).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.bot_team.conversations import (
    PASS_SENTINEL,
    ConversationStore,
    atomic_write_json,
    default_bots_root,
)
from server.bot_team.turns import run_bot_turn
from server.goal_run.bot_identity import BotRegistry, BotSpec

#: A group stays small enough that every round is cheap and readable.
MAX_GROUP_MEMBERS = 6

#: One round per user message unless the group asked for more.
DEFAULT_ROUNDS_PER_MESSAGE = 1

MAX_ROUNDS_PER_MESSAGE = 5

_GROUPS_FILE = "groups.json"

_GROUP_CONTEXT_HEADER = (
    "You are one of {count} bots in the group chat '{name}'. "
    "The transcript below is the whole conversation so far. "
    "Answer only if you can add something the other members did not already say. "
    "If you have nothing to add, reply with exactly {pass_sentinel} and nothing else."
)


@dataclass(frozen=True)
class GroupChat:
    """One group chat."""

    id: str
    name: str
    bot_ids: tuple[str, ...]
    rounds_per_message: int = DEFAULT_ROUNDS_PER_MESSAGE
    created_at: float = 0.0
    admin_bot_id: str = ""
    reply_mode: str = "everyone"
    notice: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "bot_ids": list(self.bot_ids),
            "rounds_per_message": self.rounds_per_message,
            "created_at": self.created_at,
            "admin_bot_id": self.admin_bot_id,
            "reply_mode": self.reply_mode,
            "notice": self.notice,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> GroupChat:
        return cls(
            id=str(value.get("id") or ""),
            name=str(value.get("name") or ""),
            bot_ids=tuple(str(bot_id) for bot_id in (value.get("bot_ids") or [])),
            rounds_per_message=max(
                1,
                min(
                    MAX_ROUNDS_PER_MESSAGE,
                    int(value.get("rounds_per_message") or DEFAULT_ROUNDS_PER_MESSAGE),
                ),
            ),
            created_at=float(value.get("created_at") or 0.0),
            admin_bot_id=str(value.get("admin_bot_id") or ""),
            reply_mode=str(value.get("reply_mode") or "everyone"),
            notice=str(value.get("notice") or ""),
        )



class GroupStore:
    """Groups, and each group's own transcript (never a bot's conversation)."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else default_bots_root()
        self.path = self.root / _GROUPS_FILE

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): value for key, value in raw.items() if isinstance(value, dict)}

    def _save(self, items: dict[str, dict[str, Any]]) -> None:
        atomic_write_json(self.path, items)

    def list(self) -> list[GroupChat]:
        return [GroupChat.from_dict(item) for item in self._load().values()]

    def get(self, group_id: str) -> GroupChat | None:
        raw = self._load().get(str(group_id))
        return GroupChat.from_dict(raw) if raw is not None else None

    def create(
        self,
        *,
        name: str,
        bot_ids: list[str],
        rounds_per_message: int = DEFAULT_ROUNDS_PER_MESSAGE,
        admin_bot_id: str = "",
        reply_mode: str = "everyone",
        notice: str = "",
    ) -> GroupChat:
        """Create a group. Members must be unique and within the size limit."""

        group_name = str(name or "").strip()
        if not group_name:
            raise ValueError("group name is required")
        members: list[str] = []
        for bot_id in bot_ids:
            value = str(bot_id or "").strip()
            if not value:
                raise ValueError("bot_ids must not contain empty ids")
            if value in members:
                raise ValueError(f"duplicate group member: {value}")
            members.append(value)
        if not members:
            raise ValueError("a group needs at least one member bot")
        if len(members) > MAX_GROUP_MEMBERS:
            raise ValueError(f"a group may have at most {MAX_GROUP_MEMBERS} members")
        rounds = max(1, min(MAX_ROUNDS_PER_MESSAGE, int(rounds_per_message or 1)))
        admin_id = str(admin_bot_id or "").strip()
        if admin_id and admin_id not in members:
            raise ValueError(f"admin bot {admin_id!r} must be a member of the group")
        group = GroupChat(
            id=f"grp_{uuid.uuid4().hex[:16]}",
            name=group_name[:120],
            bot_ids=tuple(members),
            rounds_per_message=rounds,
            created_at=time.time(),
            admin_bot_id=admin_id or (members[0] if members else ""),
            reply_mode=str(reply_mode or "everyone").strip().lower(),
            notice=str(notice or "").strip(),
        )
        items = self._load()
        items[group.id] = group.to_dict()
        self._save(items)
        return group

    def update(
        self,
        group_id: str,
        *,
        name: str | None = None,
        bot_ids: list[str] | None = None,
        rounds_per_message: int | None = None,
        admin_bot_id: str | None = None,
        reply_mode: str | None = None,
        notice: str | None = None,
    ) -> GroupChat | None:
        """Update metadata, members, admin, reply mode or notice of an existing group."""
        items = self._load()
        raw = items.get(str(group_id))
        if raw is None:
            return None
        current = GroupChat.from_dict(raw)

        new_members: tuple[str, ...] = current.bot_ids
        if bot_ids is not None:
            validated: list[str] = []
            for b in bot_ids:
                v = str(b or "").strip()
                if not v or v in validated:
                    continue
                validated.append(v)
            if not validated:
                raise ValueError("group needs at least one member bot")
            if len(validated) > MAX_GROUP_MEMBERS:
                raise ValueError(f"a group may have at most {MAX_GROUP_MEMBERS} members")
            new_members = tuple(validated)

        new_name = str(name).strip()[:120] if name is not None else current.name
        new_rounds = (
            max(1, min(MAX_ROUNDS_PER_MESSAGE, int(rounds_per_message)))
            if rounds_per_message is not None
            else current.rounds_per_message
        )
        new_admin = (
            str(admin_bot_id).strip() if admin_bot_id is not None else current.admin_bot_id
        )
        if new_admin and new_admin not in new_members:
            raise ValueError(f"admin bot {new_admin!r} must be a member of the group")
        new_mode = (
            str(reply_mode).strip().lower() if reply_mode is not None else current.reply_mode
        )
        new_notice = str(notice).strip() if notice is not None else current.notice

        updated = GroupChat(
            id=current.id,
            name=new_name,
            bot_ids=new_members,
            rounds_per_message=new_rounds,
            created_at=current.created_at,
            admin_bot_id=new_admin,
            reply_mode=new_mode,
            notice=new_notice,
        )
        items[updated.id] = updated.to_dict()
        self._save(items)
        return updated


    def delete(self, group_id: str) -> bool:
        items = self._load()
        if str(group_id) not in items:
            return False
        del items[str(group_id)]
        self._save(items)
        return True

    def messages_path(self, group_id: str) -> Path:
        return self.root / "groups" / str(group_id) / "messages.json"

    def append_message(
        self,
        group_id: str,
        *,
        role: str,
        text: str,
        bot_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one message to the group transcript."""

        if role not in ("user", "bot"):
            raise ValueError(f"role must be 'user' or 'bot', got {role!r}")
        body = str(text or "")
        message = {
            "id": f"msg_{uuid.uuid4().hex[:16]}",
            "group_id": str(group_id),
            "role": role,
            "bot_id": str(bot_id) if bot_id else None,
            "text": body,
            "task_id": task_id,
            "ts": time.time(),
            # Same rule as a bot conversation: a PASS reply is stored, never shown.
            "hidden": body.strip() == PASS_SENTINEL,
        }
        path = self.messages_path(group_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = []
        if not isinstance(raw, list):
            raw = []
        raw.append(message)
        atomic_write_json(path, raw)
        return message

    def messages(self, group_id: str, *, include_hidden: bool = False) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.messages_path(group_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        items = [item for item in raw if isinstance(item, dict)]
        if include_hidden:
            return items
        return [item for item in items if not item.get("hidden")]


def transcript(group: GroupChat, messages: list[dict[str, Any]]) -> str:
    """Render the group transcript that every member turn receives as context."""

    lines = [
        _GROUP_CONTEXT_HEADER.format(
            count=len(group.bot_ids),
            name=group.name,
            pass_sentinel=PASS_SENTINEL,
        ),
        "",
        "Transcript:",
    ]
    for item in messages[-40:]:
        speaker = str(item.get("bot_id") or "user")
        lines.append(f"{speaker}: {item.get('text')}")
    return "\n".join(lines)


async def run_group_turn(
    *,
    group: GroupChat,
    text: str,
    user: dict[str, Any],
    registry: BotRegistry,
    conversations: ConversationStore | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Offer every member one turn per round, in order, then stop.

    Returns the visible replies of the round plus which members passed. A
    member that no longer exists in the registry is reported as skipped rather
    than failing the whole round.
    """

    store = conversations if conversations is not None else ConversationStore()
    # Same root as the bot conversations, so one VEYA_BOTS_ROOT moves everything.
    groups = GroupStore(store.root)
    groups.append_message(group.id, role="user", text=str(text or ""))

    # A member that is gone is reported, never blocking the others.
    from server.feature_flags import enabled as flag_enabled

    if flag_enabled("VEYA_GROUP_ROLES"):
        from veya.oskill.pure.group_roles import select_replying_bots

        candidate_ids = select_replying_bots(
            reply_mode=group.reply_mode,
            admin_bot_id=group.admin_bot_id or (group.bot_ids[0] if group.bot_ids else ""),
            member_bot_ids=list(group.bot_ids),
            text=str(text or ""),
        )
    else:
        candidate_ids = list(group.bot_ids)

    available = [bot_id for bot_id in candidate_ids if registry.get(bot_id) is not None]
    replies: list[dict[str, Any]] = []
    passed: list[str] = []
    skipped: list[str] = []
    rounds_run = 0
    for _round in range(max(1, group.rounds_per_message)):
        rounds_run += 1
        for bot_id in candidate_ids:
            bot: BotSpec | None = registry.get(bot_id)
            if bot is None:
                skipped.append(bot_id)
                continue
            turn = await run_bot_turn(
                bot=bot,
                text=str(text or ""),
                context=transcript(group, groups.messages(group.id, include_hidden=True)),
                user=user,
                workspace_id=workspace_id,
                conversations=store,
            )
            # The group transcript records the reply (a PASS reply is stored
            # hidden here and in the bot's own conversation).
            groups.append_message(
                group.id,
                role="bot",
                bot_id=bot_id,
                text=turn.reply,
                task_id=turn.task_id,
            )
            if turn.reply.strip() == PASS_SENTINEL:
                if bot_id not in passed:
                    passed.append(bot_id)
                continue
            replies.append(
                {
                    "bot_id": bot_id,
                    "reply": turn.reply,
                    "task_id": turn.task_id,
                    "session_id": turn.session_id,
                    "round": rounds_run,
                }
            )
        if len(passed) >= len(available):
            # Everybody that can speak has passed: no further round adds anything.
            break
    return {
        "group_id": group.id,
        "rounds": rounds_run,
        "replies": replies,
        "passed": passed,
        "skipped": skipped,
    }


__all__ = [
    "DEFAULT_ROUNDS_PER_MESSAGE",
    "MAX_GROUP_MEMBERS",
    "MAX_ROUNDS_PER_MESSAGE",
    "PASS_SENTINEL",
    "GroupChat",
    "GroupStore",
    "run_group_turn",
    "transcript",
]
