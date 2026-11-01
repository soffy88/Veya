"""Group chats between named bots (Lorca P2).

A group is a chat room with 1..6 member bots. One user message offers every
member one turn, in the order they were added, with the group transcript as
context. A member that has nothing to add answers with the PASS sentinel; that
reply is stored hidden and is never returned by the messages endpoint. When
every member passes, the round stops early instead of spending more turns.

Groups own no execution authority: each member turn is a normal product task
for that bot (see :mod:`server.bot_team.turns`).

Reply mode ``lead`` changes the shape of a round: the admin bot plans and
delegates, the other members report, then the admin synthesizes one final
conclusion. One lead cycle runs per user message; ``rounds_per_message`` does
not multiply it.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from server.bot_team.post_budget import room_budget
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

#: Reply mode where the admin bot leads instead of peers taking turns.
LEAD_REPLY_MODE = "lead"

#: Reply mode where one clearly addressed or clearly matching member answers
#: alone, and anything ambiguous falls back to the lead cycle.
AUTO_REPLY_MODE = "auto"

# NOTE: every lead header below deliberately starts with "You are one of" and
# is paired with a "Transcript:" section: the window bridge only types a
# group-turn context block when it sees those two markers. Do not reword the
# opening or the section title without updating the bridge gating too.
_LEAD_PLAN_HEADER = (
    "You are one of {count} bots in the group chat '{name}'. You are the LEAD of "
    "this group. The workers are: {workers}. Read the transcript, then give a "
    "delegation plan: what each worker should do and what counts as done. The "
    "workers will reply after you, then you will synthesize their reports into "
    "one final conclusion. If there is nothing worth delegating, reply with "
    "exactly {pass_sentinel} and nothing else."
)

_LEAD_WORKER_HEADER = (
    "You are one of {count} bots in the group chat '{name}'. You are a WORKER. "
    "The lead is {lead}. Follow the lead's delegation plan in the transcript: "
    "do only the part assigned to you. If nothing was assigned to you, reply "
    "with exactly {pass_sentinel} and nothing else."
)

_LEAD_SYNTH_HEADER = (
    "You are one of {count} bots in the group chat '{name}'. You are the LEAD of "
    "this group. The workers have reported. Synthesize their replies into one "
    "final conclusion: what is adopted, what is rejected, what is still "
    "missing. Do not just pile up their words. If there is nothing to "
    "synthesize, reply with exactly {pass_sentinel} and nothing else."
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
    held_bot_ids: tuple[str, ...] = ()
    held_messages: dict[str, tuple[str, ...]] | None = None

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
            "held_bot_ids": list(self.held_bot_ids),
            "held_messages": {
                key: list(value)
                for key, value in (self.held_messages or {}).items()
            },
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
            held_bot_ids=tuple(
                str(bot_id) for bot_id in (value.get("held_bot_ids") or [])
            ),
            held_messages={
                str(key): tuple(str(item) for item in (value or []))
                for key, value in (value.get("held_messages") or {}).items()
                if isinstance(value, list)
            },
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
        held_bot_ids: list[str] | None = None,
        held_messages: dict[str, list[str]] | None = None,
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
        new_admin = str(admin_bot_id).strip() if admin_bot_id is not None else current.admin_bot_id
        if new_admin and new_admin not in new_members:
            raise ValueError(f"admin bot {new_admin!r} must be a member of the group")
        new_mode = str(reply_mode).strip().lower() if reply_mode is not None else current.reply_mode
        new_notice = str(notice).strip() if notice is not None else current.notice
        new_held = (
            tuple(str(bot_id) for bot_id in held_bot_ids)
            if held_bot_ids is not None
            else current.held_bot_ids
        )
        new_stash = (
            {str(key): tuple(value) for key, value in held_messages.items()}
            if held_messages is not None
            else current.held_messages
        )

        updated = GroupChat(
            id=current.id,
            name=new_name,
            bot_ids=new_members,
            rounds_per_message=new_rounds,
            created_at=current.created_at,
            admin_bot_id=new_admin,
            reply_mode=new_mode,
            notice=new_notice,
            held_bot_ids=new_held,
            held_messages=new_stash,
        )
        items[updated.id] = updated.to_dict()
        self._save(items)
        return updated

    def stash_held_message(self, group_id: str, bot_id: str, text: str) -> None:
        """Stash one room message for a held member (cap 20, oldest dropped)."""

        from server.bot_team.hold_directives import STASH_CAP

        body = str(text or "").strip()
        if not body:
            return
        items = self._load()
        raw = items.get(str(group_id))
        if raw is None:
            return
        group = GroupChat.from_dict(raw)
        if bot_id not in group.held_bot_ids:
            return
        stashed = dict(group.held_messages or {})
        queue = list(stashed.get(bot_id, ())) + [body]
        stashed[bot_id] = tuple(queue[-STASH_CAP:])
        items[group.id] = replace(group, held_messages=stashed).to_dict()
        self._save(items)

    def held_context(self, group_id: str, bot_id: str) -> list[str]:
        """Stashed messages waiting for a held member (peek, no clear)."""

        group = self.get(str(group_id))
        if group is None:
            return []
        return list((group.held_messages or {}).get(bot_id, ()))

    def clear_held_context(self, group_id: str, bot_id: str) -> None:
        """Drop a member's stash after its turn consumed it."""

        items = self._load()
        raw = items.get(str(group_id))
        if raw is None:
            return
        group = GroupChat.from_dict(raw)
        stashed = dict(group.held_messages or {})
        if bot_id not in stashed:
            return
        del stashed[bot_id]
        items[group.id] = replace(group, held_messages=stashed).to_dict()
        self._save(items)

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
        reply_to: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one message to the group transcript."""

        if role not in ("user", "bot"):
            raise ValueError(f"role must be 'user' or 'bot', got {role!r}")
        body = str(text or "")
        try:
            from server.feature_flags import enabled as _flag_enabled

            if _flag_enabled("VEYA_BOT_SECRETS"):
                from server.bot_team.secrets import bot_secret_store
                from veya.oskill.pure.secret_masking import mask_secret_values

                mask_map = bot_secret_store().get_masking_map()
                if mask_map:
                    body = mask_secret_values(body, mask_map)
        except Exception:
            pass
        message = {
            "id": f"msg_{uuid.uuid4().hex[:16]}",
            "group_id": str(group_id),
            "role": role,
            "bot_id": str(bot_id) if bot_id else None,
            "text": body,
            "task_id": task_id,
            "ts": time.time(),
            # Same rule as a bot conversation: a PASS reply is stored, never shown.
            "hidden": body.strip() in ("", PASS_SENTINEL),
        }
        if reply_to is not None:
            message["reply_to"] = dict(reply_to)
        from server.bot_team.rev import bump_rev

        message["rev"] = bump_rev(root=self.root)
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


def _lead_plan_header(group: GroupChat, worker_ids: list[str]) -> str:
    return _LEAD_PLAN_HEADER.format(
        count=len(group.bot_ids),
        name=group.name,
        workers=", ".join(worker_ids) if worker_ids else "none",
        pass_sentinel=PASS_SENTINEL,
    )


def _lead_worker_header(group: GroupChat, lead_id: str) -> str:
    return _LEAD_WORKER_HEADER.format(
        count=len(group.bot_ids),
        name=group.name,
        lead=lead_id,
        pass_sentinel=PASS_SENTINEL,
    )


def _lead_synth_header(group: GroupChat) -> str:
    return _LEAD_SYNTH_HEADER.format(
        count=len(group.bot_ids),
        name=group.name,
        pass_sentinel=PASS_SENTINEL,
    )


def transcript(
    group: GroupChat,
    messages: list[dict[str, Any]],
    *,
    header: str | None = None,
) -> str:
    """Render the group transcript that every member turn receives as context."""

    if header is None:
        header = _GROUP_CONTEXT_HEADER.format(
            count=len(group.bot_ids),
            name=group.name,
            pass_sentinel=PASS_SENTINEL,
        )
    lines = [
        header,
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
    reply_to: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Offer every member one turn per round, in order, then stop.

    Returns the visible replies of the round plus which members passed. A
    member that no longer exists in the registry is reported as skipped rather
    than failing the whole round.
    """

    store = conversations if conversations is not None else ConversationStore()
    groups = GroupStore(store.root)
    # The store is the source of truth: a caller may pass a group object
    # read before a hold changed it.
    stored = groups.get(group.id)
    if stored is not None:
        group = stored
    # Room-member holds (OpenMausBot room-holds method): "@beta stop" holds
    # a member, "@beta resume" releases one, a bare resume word releases
    # all. A directive is control traffic, not content: it changes state
    # and ends the round with no member turns.
    from server.bot_team.hold_directives import classify_hold_directive

    directive = classify_hold_directive(
        str(text or ""), list(group.bot_ids), registry
    )
    if directive is not None:
        groups.append_message(
            group.id, role="user", text=str(text or ""), reply_to=reply_to
        )
        held, resumed = _apply_hold_directive(groups, group, directive)
        return {
            "group_id": group.id,
            "rounds": 1,
            "replies": [],
            "passed": [],
            "skipped": [],
            "held": held,
            "resumed": resumed,
        }
    # A human spoke: the post budget lifts its breaker and resets the
    # unanswered streak for this room.
    room_budget(group.id).note_user_message()
    # Content for held members is stashed and delivered when they are next
    # selected for a turn; held members never speak until released.
    held_now = [bot_id for bot_id in group.held_bot_ids if bot_id in group.bot_ids]
    for bot_id in held_now:
        groups.stash_held_message(group.id, bot_id, str(text or ""))
    effective = group
    if held_now:
        effective = replace(
            group,
            bot_ids=tuple(bot_id for bot_id in group.bot_ids if bot_id not in held_now),
        )
    _mode = (effective.reply_mode or "everyone").strip().lower()
    if _mode == AUTO_REPLY_MODE:
        return await run_auto_turn(
            group=effective,
            text=text,
            user=user,
            registry=registry,
            conversations=store,
            workspace_id=workspace_id,
            reply_to=reply_to,
        )
    if (effective.reply_mode or "everyone").strip().lower() == LEAD_REPLY_MODE:
        return await run_lead_turn(
            group=effective,
            text=text,
            user=user,
            registry=registry,
            conversations=store,
            workspace_id=workspace_id,
            reply_to=reply_to,
        )
    # From here the filtered copy is the group: held members are never
    # candidates, in either the roles branch or the plain list below.
    group = effective
    groups.append_message(group.id, role="user", text=str(text or ""), reply_to=reply_to)

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
    blocked: list[str] = []
    rounds_run = 0
    visible_rounds = 0
    spoken: set[str] = set()
    round_candidates = list(candidate_ids)
    for _round in range(max(1, group.rounds_per_message)):
        if not round_candidates:
            break
        # Rotate the starting speaker each round.
        if _round and len(round_candidates) > 1:
            shift = _round % len(round_candidates)
            round_candidates = round_candidates[shift:] + round_candidates[:shift]
        rounds_run += 1
        round_replies: list[str] = []
        round_visible = False
        for bot_id in round_candidates:
            bot: BotSpec | None = registry.get(bot_id)
            if bot is None:
                skipped.append(bot_id)
                continue
            stashed = groups.held_context(group.id, bot_id)
            context = transcript(group, groups.messages(group.id, include_hidden=True))
            if stashed:
                context = (
                    "[你在被 hold 期间收到的房内消息，先处理这些：]\n"
                    + "\n".join(f"- {item}" for item in stashed)
                    + "\n\n"
                    + context
                )
            turn = await run_bot_turn(
                bot=bot,
                text=str(text or ""),
                context=context,
                user=user,
                workspace_id=workspace_id,
                conversations=store,
                thread_id=group.id,
            )
            if stashed:
                groups.clear_held_context(group.id, bot_id)
            # Round 1 answers the user, not a mention: only speech in a
            # chain round (round 2+) excludes a member from later rounds.
            # A pass excludes in any round (handled via `passed` below).
            if _round >= 1:
                spoken.add(bot_id)
            # The group transcript records the reply (a PASS reply is stored
            # hidden here and in the bot's own conversation).
            if turn.reply.strip() in ("", PASS_SENTINEL):
                groups.append_message(
                    group.id,
                    role="bot",
                    bot_id=bot_id,
                    # An empty reply is a pass: store the sentinel so it
                    # stays hidden, exactly like an explicit pass.
                    text=turn.reply if turn.reply.strip() else PASS_SENTINEL,
                    task_id=turn.task_id,
                )
                if bot_id not in passed:
                    passed.append(bot_id)
                continue
            # The post budget judges every visible post before it lands: a
            # refused post is never stored and never counts as a round.
            decision = room_budget(group.id).decide(bot_id, turn.reply)
            if not decision.allowed:
                if bot_id not in blocked:
                    blocked.append(bot_id)
                if decision.escalate:
                    _escalate_attention(groups, group, bot_id, decision.reason)
                continue
            groups.append_message(
                group.id,
                role="bot",
                bot_id=bot_id,
                text=turn.reply,
                task_id=turn.task_id,
            )
            round_visible = True
            round_replies.append(turn.reply)
            replies.append(
                {
                    "bot_id": bot_id,
                    "reply": turn.reply,
                    "task_id": turn.task_id,
                    "session_id": turn.session_id,
                    "round": rounds_run,
                }
            )
        if round_visible:
            visible_rounds += 1
        if len(passed) >= len(available):
            # Everybody that can speak has passed: no further round adds anything.
            break
        # Mention chain: the next round calls only members newly @-mentioned
        # in this round's replies who have not spoken or passed yet.
        round_candidates = [
            bot_id
            for bot_id in mentioned_bot_ids(round_replies, available, registry)
            if bot_id not in spoken and bot_id not in passed
        ]
    return {
        "group_id": group.id,
        # A round that only passed consumes no round: the count is the
        # rounds that produced a visible reply, floored at 1 as before.
        "rounds": max(1, visible_rounds),
        "replies": replies,
        "passed": passed,
        "skipped": skipped,
        "blocked": blocked,
    }


def _apply_hold_directive(
    groups: GroupStore, group: GroupChat, directive: Any
) -> tuple[list[str], list[str]]:
    """Apply a parsed directive to the group record; return (held, resumed)."""

    current = [bot_id for bot_id in group.held_bot_ids if bot_id in group.bot_ids]
    if directive.resume_all:
        resumed = list(current)
        groups.update(group.id, held_bot_ids=[])
        return [], resumed
    held = list(current)
    resumed: list[str] = []
    for bot_id in directive.resumes:
        if bot_id in held:
            held.remove(bot_id)
            resumed.append(bot_id)
    for bot_id in directive.holds:
        if bot_id not in held and bot_id in group.bot_ids:
            held.append(bot_id)
    groups.update(group.id, held_bot_ids=held)
    return held, resumed


def _escalate_attention(
    groups: GroupStore, group: GroupChat, bot_id: str, reason: str
) -> None:
    """Hand an unanswered round to a human; never fail the round itself."""

    try:
        from server.bot_team.attention import AttentionStore

        AttentionStore(groups.root).create(
            question=(
                f"房内发帖无人接：{bot_id} 在 {group.name} 连发无人接，"
                "自动暂停发帖 5 分钟，等你定夺。"
            ),
            group_id=group.id,
            kind="escalation",
            context=f"reason={reason}",
        )
    except Exception:
        pass


def mentioned_bot_ids(
    texts: list[str],
    available: list[str],
    registry: Any,
) -> list[str]:
    """Bot ids newly @-mentioned in *texts*, in first-mention order.

    A mention is ``@`` + bot_id or display name, bounded by
    non-alphanumeric characters on both sides -- the same discipline as
    :func:`select_lead_owner`, so ``@beta`` matches but ``@beta2`` and
    ``beta@example`` do not.
    """

    found: list[str] = []
    for text in texts:
        body = str(text or "")
        hits: list[tuple[int, str]] = []
        for bot_id in available:
            if bot_id in found:
                continue
            spec = registry.get(bot_id)
            tokens = {bot_id}
            if spec is not None and getattr(spec, "name", ""):
                tokens.add(str(spec.name))
            earliest: int | None = None
            for token in tokens:
                pattern = re.compile(
                    r"(?<![A-Za-z0-9_])@" + re.escape(token) + r"(?![A-Za-z0-9_])",
                    re.IGNORECASE,
                )
                match = pattern.search(body)
                if match is not None and (earliest is None or match.start() < earliest):
                    earliest = match.start()
            if earliest is not None:
                hits.append((earliest, bot_id))
        hits.sort()
        found.extend(bot_id for _, bot_id in hits)
    return found


def select_lead_owner(
    *,
    text: str,
    available: list[str],
    registry: BotRegistry,
) -> str | None:
    """Deterministic owner routing for lead mode (OpenBot method, no model).

    Returns a bot_id only in two unambiguous cases: the message names
    exactly one available member (by bot_id or display name, bounded by
    non-alphanumeric characters, so "Museb\u8bf4" matches but "Museb2" does
    not), or exactly one member is available. Anything else returns None
    and the caller runs the full lead cycle. A named member that is not
    available is ignored, never broadcast; overlapping name matches keep
    only the longest span, so a prefix name cannot fake ambiguity.
    """

    if len(available) == 1:
        return available[0]
    spans: list[tuple[int, int, str]] = []
    for bot_id in available:
        bot = registry.get(bot_id)
        if bot is None:
            continue
        names = {bot_id}
        if bot.name:
            names.add(bot.name)
        for name in names:
            pattern = r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])"
            for match in re.finditer(pattern, text):
                spans.append((match.start(), match.end(), bot_id))
    if not spans:
        return None
    spans.sort(key=lambda item: (item[1] - item[0]), reverse=True)
    kept: list[tuple[int, int, str]] = []
    for start, end, bot_id in spans:
        if all(end <= kept_start or start >= kept_end for kept_start, kept_end, _ in kept):
            kept.append((start, end, bot_id))
    owners = {bot_id for _, _, bot_id in kept}
    if len(owners) != 1:
        return None
    return owners.pop()


async def run_lead_turn(
    *,
    group: GroupChat,
    text: str,
    user: dict[str, Any],
    registry: BotRegistry,
    conversations: ConversationStore | None = None,
    workspace_id: str | None = None,
    _skip_append: bool = False,
    reply_to: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One lead cycle per user message: lead plans, workers report, lead synthesizes.

    Returns the same shape as :func:`run_group_turn`. A member that no longer
    exists in the registry is reported as skipped rather than failing the
    cycle; if the admin is gone, the first available member acts as lead.
    ``rounds_per_message`` does not multiply a lead cycle.

    Deterministic routing runs before the cycle: a message that names
    exactly one available member, or a room with exactly one available
    member, goes straight to that member as a single turn (phase
    ``"direct"``) instead of plan/report/synthesis. Anything ambiguous
    falls through to the full cycle; no model is involved in routing.
    """

    store = conversations if conversations is not None else ConversationStore()
    groups = GroupStore(store.root)
    if not _skip_append:
        groups.append_message(group.id, role="user", text=str(text or ""), reply_to=reply_to)

    available = [bot_id for bot_id in group.bot_ids if registry.get(bot_id) is not None]
    skipped = [bot_id for bot_id in group.bot_ids if registry.get(bot_id) is None]
    replies: list[dict[str, Any]] = []
    passed: list[str] = []
    if not available:
        return {
            "group_id": group.id,
            "rounds": 1,
            "replies": replies,
            "passed": passed,
            "skipped": skipped,
        }
    lead_id = group.admin_bot_id if group.admin_bot_id in available else available[0]
    worker_ids = [bot_id for bot_id in available if bot_id != lead_id]

    async def _turn(bot_id: str, header: str | None):
        bot = registry.get(bot_id)
        assert bot is not None
        stashed = groups.held_context(group.id, bot_id)
        context = transcript(
            group,
            groups.messages(group.id, include_hidden=True),
            header=header,
        )
        if stashed:
            context = (
                "[你在被 hold 期间收到的房内消息，先处理这些：]\n"
                + "\n".join(f"- {item}" for item in stashed)
                + "\n\n"
                + context
            )
        turn = await run_bot_turn(
            bot=bot,
            text=str(text or ""),
            context=context,
            user=user,
            workspace_id=workspace_id,
            conversations=store,
            thread_id=group.id,
        )
        if stashed:
            groups.clear_held_context(group.id, bot_id)
        groups.append_message(
            group.id,
            role="bot",
            bot_id=bot_id,
            text=turn.reply if turn.reply.strip() else PASS_SENTINEL,
            task_id=turn.task_id,
        )
        return turn

    # Deterministic owner routing (no model): a directly addressed member,
    # or the sole available member, answers alone. Ambiguity falls through
    # to the full lead cycle below.
    owner_id = select_lead_owner(text=str(text or ""), available=available, registry=registry)
    if owner_id is not None:
        turn = await _turn(owner_id, None)
        if turn.reply.strip() in ("", PASS_SENTINEL):
            passed.append(owner_id)
        else:
            replies.append(
                {
                    "bot_id": owner_id,
                    "reply": turn.reply,
                    "task_id": turn.task_id,
                    "session_id": turn.session_id,
                    "round": 1,
                    "phase": "direct",
                }
            )
        return {
            "group_id": group.id,
            "rounds": 1,
            "replies": replies,
            "passed": passed,
            "skipped": skipped,
        }
    plan = await _turn(lead_id, _lead_plan_header(group, worker_ids))
    if plan.reply.strip() in ("", PASS_SENTINEL):
        passed.append(lead_id)
    else:
        replies.append(
            {
                "bot_id": lead_id,
                "reply": plan.reply,
                "task_id": plan.task_id,
                "session_id": plan.session_id,
                "round": 1,
                "phase": "plan",
            }
        )
    for bot_id in worker_ids:
        turn = await _turn(bot_id, _lead_worker_header(group, lead_id))
        if turn.reply.strip() in ("", PASS_SENTINEL):
            if bot_id not in passed:
                passed.append(bot_id)
            continue
        replies.append(
            {
                "bot_id": bot_id,
                "reply": turn.reply,
                "task_id": turn.task_id,
                "session_id": turn.session_id,
                "round": 1,
                "phase": "report",
            }
        )
    synth = await _turn(lead_id, _lead_synth_header(group))
    if synth.reply.strip() in ("", PASS_SENTINEL):
        if lead_id not in passed:
            passed.append(lead_id)
    else:
        replies.append(
            {
                "bot_id": lead_id,
                "reply": synth.reply,
                "task_id": synth.task_id,
                "session_id": synth.session_id,
                "round": 1,
                "phase": "synthesis",
            }
        )
    return {
        "group_id": group.id,
        "rounds": 1,
        "replies": replies,
        "passed": passed,
        "skipped": skipped,
    }


#: Words too generic to route on (English function words + common Chinese
#: particles). Everything else in the message is a routing keyword.
_AUTO_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into", "about",
        "please", "help", "want", "need", "what", "when", "where", "which",
        "there", "here", "your", "yours", "have", "has", "been", "will",
        "would", "could", "should", "does", "doing", "done", "make", "take",
        "帮我", "请帮", "一下", "怎么", "什么", "我的", "我们", "你们",
        "这个", "那个", "这里", "那里", "然后", "还有", "以及", "或者",
    }
)


def _auto_keywords(text: str) -> set[str]:
    """Routing keywords: English words (len>=3) + Chinese 2-char windows."""

    lowered = str(text or "").lower()
    words: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", lowered):
        if len(token) >= 3 and token not in _AUTO_STOPWORDS:
            words.add(token)
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(run) <= 4:
            words.add(run)
        for i in range(len(run) - 1):
            pair = run[i : i + 2]
            if pair not in _AUTO_STOPWORDS:
                words.add(pair)
    return words


def select_auto_responder(
    text: str, *, available: list[str], registry: BotRegistry
) -> str | None:
    """Pick the one member whose job clearly matches the message, or None.

    A keyword scores 3 in the bot id/name, 2 in its description, 1 in its
    instructions. The winner needs at least 4 points and a margin of 2 over
    the runner-up; anything closer is ambiguous and falls back to the lead
    cycle. Deterministic: no model, no randomness.
    """

    keywords = _auto_keywords(text)
    if not keywords or not available:
        return None
    scored: list[tuple[int, str]] = []
    for bot_id in available:
        spec = registry.get(bot_id)
        if spec is None:
            continue
        name = f"{spec.bot_id} {spec.name or ''}".lower()
        desc = (spec.description or "").lower()
        instr = (spec.instructions or "").lower()
        score = 0
        for keyword in keywords:
            if keyword in name:
                score += 3
            elif keyword in desc:
                score += 2
            elif keyword in instr:
                score += 1
        scored.append((score, bot_id))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))
    best, second = scored[0], (scored[1] if len(scored) > 1 else (0, ""))
    if best[0] >= 4 and best[0] - second[0] >= 2:
        return best[1]
    return None


async def run_auto_turn(
    *,
    group: GroupChat,
    text: str,
    user: dict[str, Any],
    registry: BotRegistry,
    conversations: ConversationStore | None = None,
    workspace_id: str | None = None,
    reply_to: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Auto reply mode: direct answer when the addressee is clear, else lead.

    Order: (1) a message naming exactly one available member goes straight
    to that member (phase ``"direct"``, same rule as the lead mode);
    (2) a message whose keywords clearly match one member's job goes to
    that member alone (phase ``"auto"``); (3) anything ambiguous runs the
    full lead cycle (plan/report/synthesis), with the user message
    appended exactly once. Returns the same shape as :func:`run_lead_turn`.
    """

    store = conversations if conversations is not None else ConversationStore()
    groups = GroupStore(store.root)
    groups.append_message(group.id, role="user", text=str(text or ""), reply_to=reply_to)

    available = [bot_id for bot_id in group.bot_ids if registry.get(bot_id) is not None]
    skipped = [bot_id for bot_id in group.bot_ids if registry.get(bot_id) is None]
    replies: list[dict[str, Any]] = []
    passed: list[str] = []
    if not available:
        return {
            "group_id": group.id,
            "rounds": 1,
            "replies": replies,
            "passed": passed,
            "skipped": skipped,
        }

    target_id = select_lead_owner(text=str(text or ""), available=available, registry=registry)
    phase = "direct"
    if target_id is None:
        target_id = select_auto_responder(
            str(text or ""), available=available, registry=registry
        )
        phase = "auto"
    if target_id is not None:
        bot = registry.get(target_id)
        assert bot is not None
        stashed = groups.held_context(group.id, target_id)
        context = transcript(group, groups.messages(group.id, include_hidden=True))
        if stashed:
            context = (
                "[你在被 hold 期间收到的房内消息，先处理这些：]\n"
                + "\n".join(f"- {item}" for item in stashed)
                + "\n\n"
                + context
            )
        turn = await run_bot_turn(
            bot=bot,
            text=str(text or ""),
            context=context,
            user=user,
            workspace_id=workspace_id,
            conversations=store,
            thread_id=group.id,
        )
        if stashed:
            groups.clear_held_context(group.id, target_id)
        groups.append_message(
            group.id,
            role="bot",
            bot_id=target_id,
            text=turn.reply if turn.reply.strip() else PASS_SENTINEL,
            task_id=turn.task_id,
        )
        if turn.reply.strip() in ("", PASS_SENTINEL):
            passed.append(target_id)
        else:
            replies.append(
                {
                    "bot_id": target_id,
                    "reply": turn.reply,
                    "task_id": turn.task_id,
                    "session_id": turn.session_id,
                    "round": 1,
                    "phase": phase,
                }
            )
        return {
            "group_id": group.id,
            "rounds": 1,
            "replies": replies,
            "passed": passed,
            "skipped": skipped,
        }

    # Ambiguous: the lead cycle decides. The user message is already in the
    # transcript, so the cycle must not append it a second time.
    return await run_lead_turn(
        group=group,
        text=text,
        user=user,
        registry=registry,
        conversations=store,
        workspace_id=workspace_id,
        _skip_append=True,
        reply_to=reply_to,
    )


__all__ = [
    "DEFAULT_ROUNDS_PER_MESSAGE",
    "LEAD_REPLY_MODE",
    "AUTO_REPLY_MODE",
    "mentioned_bot_ids",
    "run_auto_turn",
    "select_auto_responder",
    "select_lead_owner",
    "MAX_GROUP_MEMBERS",
    "MAX_ROUNDS_PER_MESSAGE",
    "PASS_SENTINEL",
    "GroupChat",
    "GroupStore",
    "run_group_turn",
    "run_lead_turn",
    "transcript",
]
