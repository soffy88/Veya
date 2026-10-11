"""Bot product foundation (B1) — identity / configuration stores.

These stores give a persistent Veya Bot its own product surface:

    BotSpec        identity + configuration (server.goal_run.bot_identity)
    BotComputer    the bot's private persistent computer
    BotConversation main / side conversation spaces
    BotMessage     ordered messages inside a conversation
    BotGoal        long-term goal definition + GoalRun *references* only
    BotSession     binding of a conversation to a bot runtime instance
    BotStatus      aggregation projection

Authority boundary (frozen): these are identity / configuration stores. They
own NO execution authority. A ``BotGoal`` stores the long-term objective and
an index of GoalRun ids; the actual run state lives entirely in GoalRun.

Every record is bot-scoped and cross-bot access is refused fail-closed.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.bot_scope import CrossBotAccessDenied, require_same_bot


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class BotComputerStatus:
    """Bot-facing computer status vocabulary (product view).

    These are *not* the authority states. The authority lifecycle lives on
    ``PersistentComputer.lifecycle_state`` (``ComputerLifecycleState``); this
    enum is only the Bot product's projection of it.
    """

    PROVISIONING = "PROVISIONING"
    READY = "READY"
    RUNNING = "RUNNING"
    SLEEPING = "SLEEPING"
    RECOVERING = "RECOVERING"
    UNAVAILABLE = "UNAVAILABLE"
    DESTROYED = "DESTROYED"


#: Authority lifecycle state -> Bot-facing product status.
_COMPUTER_STATE_TO_BOT_STATUS: dict[str, str] = {
    "created": BotComputerStatus.PROVISIONING,
    "running": BotComputerStatus.RUNNING,
    "stopped": BotComputerStatus.READY,
    "checkpointing": BotComputerStatus.RECOVERING,
    "restoring": BotComputerStatus.RECOVERING,
    "terminated": BotComputerStatus.DESTROYED,
    "failed": BotComputerStatus.UNAVAILABLE,
}


class BotGoalStatus:
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    WAITING = "WAITING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ARCHIVED = "ARCHIVED"


@dataclass
class BotComputer:
    """Bot-facing projection of the bot's persistent computer.

    AUTHORITY BOUNDARY (frozen): this is a read-only DTO projected from
    ``runtime.computer.models.PersistentComputer``. It is **not** a store and
    **not** a lifecycle authority::

        PersistentComputerStore = sole computer authority
        BotComputer              = Bot-facing projection (this class)

    Every field is derived from the authoritative record; nothing here may be
    persisted independently. Cross-bot access is refused fail-closed.
    """

    computer_id: str
    bot_id: str
    workspace_root: str = ""
    home_root: str = ""
    filesystem_root: str = ""
    browser_session: str | None = None
    terminal_session: str | None = None
    runtime_id: str = ""
    executor_binding: str | None = None
    status: str = BotComputerStatus.PROVISIONING
    created_at: str = ""
    last_seen: str = ""
    goal_run_refs: list[str] = field(default_factory=list)
    checkpoint_ref: str | None = None

    @classmethod
    def from_persistent(cls, computer: Any, *, runtime_id: str = "") -> BotComputer:
        """Project an authoritative ``PersistentComputer`` into the Bot view."""
        lifecycle = str(getattr(computer, "lifecycle_state", "") or "")
        checkpoint = getattr(computer, "checkpoint_ref", None)
        return cls(
            computer_id=computer.computer_id,
            bot_id=computer.bot_id,
            workspace_root=computer.workspace_ref,
            filesystem_root=computer.workspace_ref,
            browser_session=computer.browser_profile_ref,
            runtime_id=runtime_id,
            status=_COMPUTER_STATE_TO_BOT_STATUS.get(lifecycle, BotComputerStatus.UNAVAILABLE),
            created_at=computer.created_at,
            last_seen=computer.last_active_at,
            goal_run_refs=list(computer.goal_run_refs or []),
            checkpoint_ref=checkpoint.path if checkpoint is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BotConversation:
    conversation_id: str
    bot_id: str
    type: str = "main"  # main | side
    title: str = ""
    active_goal_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BotConversation:
        return cls(
            conversation_id=str(value.get("conversation_id") or ""),
            bot_id=str(value.get("bot_id") or ""),
            type=str(value.get("type") or "main"),
            title=str(value.get("title") or ""),
            active_goal_ids=list(value.get("active_goal_ids") or []),
            created_at=str(value.get("created_at") or _now_iso()),
            updated_at=str(value.get("updated_at") or _now_iso()),
        )


@dataclass
class BotMessage:
    message_id: str
    bot_id: str
    conversation_id: str
    sender: str  # user | bot | system
    content: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    sequence: int = 0  # per-conversation monotonic ordering

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BotMessage:
        return cls(
            message_id=str(value.get("message_id") or ""),
            bot_id=str(value.get("bot_id") or ""),
            conversation_id=str(value.get("conversation_id") or ""),
            sender=str(value.get("sender") or "user"),
            content=str(value.get("content") or ""),
            attachments=list(value.get("attachments") or []),
            created_at=str(value.get("created_at") or _now_iso()),
            sequence=int(value.get("sequence") or 0),
        )


@dataclass
class BotGoal:
    """Long-term goal definition + GoalRun *index* (never execution state)."""

    goal_id: str
    bot_id: str
    title: str = ""
    objective: str = ""
    constraints: list[str] = field(default_factory=list)
    status: str = BotGoalStatus.ACTIVE
    plan_ref: str | None = None
    memory_ref: str | None = None
    cadence: str | None = None
    trigger_ref: str | None = None
    #: Index of GoalRun ids produced for this goal. Reference only — the run
    #: state itself lives entirely in the GoalRun durable authority.
    goal_run_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BotGoal:
        return cls(
            goal_id=str(value.get("goal_id") or ""),
            bot_id=str(value.get("bot_id") or ""),
            title=str(value.get("title") or ""),
            objective=str(value.get("objective") or ""),
            constraints=list(value.get("constraints") or []),
            status=str(value.get("status") or BotGoalStatus.ACTIVE),
            plan_ref=value.get("plan_ref"),
            memory_ref=value.get("memory_ref"),
            cadence=value.get("cadence"),
            trigger_ref=value.get("trigger_ref"),
            goal_run_ids=list(value.get("goal_run_ids") or []),
            created_at=str(value.get("created_at") or _now_iso()),
            updated_at=str(value.get("updated_at") or _now_iso()),
        )


@dataclass
class BotSession:
    session_id: str
    bot_id: str
    conversation_id: str
    instance_id: str = ""
    status: str = "active"
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BotSession:
        return cls(
            session_id=str(value.get("session_id") or ""),
            bot_id=str(value.get("bot_id") or ""),
            conversation_id=str(value.get("conversation_id") or ""),
            instance_id=str(value.get("instance_id") or ""),
            status=str(value.get("status") or "active"),
            created_at=str(value.get("created_at") or _now_iso()),
            updated_at=str(value.get("updated_at") or _now_iso()),
        )


@dataclass
class BotStatus:
    bot_id: str
    lifecycle_status: str = "created"
    computer_status: str = ""
    active_goal_count: int = 0
    active_session_count: int = 0
    conversation_count: int = 0
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Stores ──────────────────────────────────────────────────────────


def default_bots_root() -> Path:
    return Path(os.environ.get("VEYA_BOTS_ROOT", str(Path.home() / ".veya" / "bots"))).expanduser()


def default_computer_store(root: str | Path | None = None) -> Any:
    """The sole computer authority store, rooted beside the bot product data.

    One SQLite substrate for every bot (not one database per bot): the store
    already scopes and isolates by ``bot_id`` and refuses cross-bot reuse of a
    computer id.
    """
    from runtime.computer.store import PersistentComputerStore

    base = Path(root) if root is not None else default_bots_root()
    return PersistentComputerStore(base / "persistent_computers.sqlite3")


class _BotDirStore:
    """Per-bot JSON file store with fail-closed cross-bot isolation."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_bots_root()

    def _bot_dir(self, bot_id: str) -> Path:
        return self.root / bot_id

    def _read(self, bot_id: str, name: str, default: Any) -> Any:
        path = self._bot_dir(bot_id) / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return default

    def _write(self, bot_id: str, name: str, data: Any) -> None:
        directory = self._bot_dir(bot_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)


class BotComputerProjection:
    """Read/write facade over the sole computer authority.

    AUTHORITY BOUNDARY (frozen): every read and write goes through
    ``runtime.computer.store.PersistentComputerStore``. This class owns no
    state and no lifecycle: it only projects ``PersistentComputer`` records
    into the Bot-facing :class:`BotComputer` DTO and refuses cross-bot access
    fail-closed. There is no second computer store.
    """

    def __init__(self, store: Any, *, runtime_id: str = "") -> None:
        self._store = store
        self._runtime_id = runtime_id

    @property
    def store(self) -> Any:
        """The authoritative :class:`PersistentComputerStore`."""
        return self._store

    def get(self, bot_id: str) -> BotComputer | None:
        """Project the bot's computer, or ``None`` when it has none yet."""
        computers = self._store.list_computers(bot_id=bot_id)
        if not computers:
            return None
        # newest by last_active_at (store orders by that already)
        return BotComputer.from_persistent(computers[0], runtime_id=self._runtime_id)

    def get_persistent(self, computer_id: str, *, bot_id: str) -> Any:
        """Fetch the authoritative record, refusing cross-bot access."""
        return self._store.get_computer(computer_id, bot_id=bot_id)

    def require(self, bot_id: str, computer_id: str) -> BotComputer:
        """Fetch + project, raising when absent or owned by another bot."""
        computer = self._store.get_computer(computer_id, bot_id=bot_id)
        if computer is None:
            raise CrossBotAccessDenied(f"computer:{computer_id}", bot_id, bot_id)
        return BotComputer.from_persistent(computer, runtime_id=self._runtime_id)

    def ensure(
        self,
        *,
        bot_id: str,
        owner_id: str,
        workspace_ref: str,
        browser_profile_ref: str | None = None,
        computer_id: str | None = None,
    ) -> Any:
        """Idempotently create the bot's computer in the authority store."""
        return self._store.create_computer(
            owner_id=owner_id,
            workspace_ref=workspace_ref,
            browser_profile_ref=browser_profile_ref,
            computer_id=computer_id,
            bot_id=bot_id,
        )

    def set_state(self, computer_id: str, state: str) -> Any:
        """Set the authoritative lifecycle state (authority vocabulary)."""
        return self._store.set_state(computer_id, state)


class BotConversationStore(_BotDirStore):
    def create(
        self, bot_id: str, *, type: str = "main", title: str = "", conversation_id: str = ""
    ) -> BotConversation:
        conversations = self._all(bot_id)
        if type == "main" and any(c.type == "main" for c in conversations):
            raise ValueError(f"bot {bot_id!r} already has a main conversation")
        conversation = BotConversation(
            conversation_id=conversation_id or _new_id("conv"),
            bot_id=bot_id,
            type=type,
            title=title,
        )
        conversations.append(conversation)
        self._write(bot_id, "conversations.json", [c.to_dict() for c in conversations])
        return conversation

    def _all(self, bot_id: str) -> list[BotConversation]:
        data = self._read(bot_id, "conversations.json", [])
        return [BotConversation.from_dict(item) for item in data if isinstance(item, dict)]

    def get(self, bot_id: str, conversation_id: str) -> BotConversation | None:
        for conversation in self._all(bot_id):
            if conversation.conversation_id == conversation_id:
                return conversation
        return None

    def require(self, bot_id: str, conversation_id: str) -> BotConversation:
        conversation = self.get(bot_id, conversation_id)
        if conversation is None:
            raise KeyError(f"conversation {conversation_id!r} not found for bot {bot_id!r}")
        # A conversation's owner is its bot_id; a mismatch can only come from a
        # lookup under the wrong bot.
        require_same_bot(bot_id, conversation.bot_id, f"conversation:{conversation_id}")
        return conversation

    def list(self, bot_id: str, *, type: str | None = None) -> list[BotConversation]:
        items = self._all(bot_id)
        if type is not None:
            items = [c for c in items if c.type == type]
        return items

    def main(self, bot_id: str) -> BotConversation | None:
        for conversation in self._all(bot_id):
            if conversation.type == "main":
                return conversation
        return None


class BotMessageStore(_BotDirStore):
    def append(
        self,
        bot_id: str,
        conversation_id: str,
        *,
        sender: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
        message_id: str = "",
    ) -> BotMessage:
        messages = self._all(bot_id)
        sequence = (
            max(
                (m.sequence for m in messages if m.conversation_id == conversation_id),
                default=0,
            )
            + 1
        )
        message = BotMessage(
            message_id=message_id or _new_id("msg"),
            bot_id=bot_id,
            conversation_id=conversation_id,
            sender=sender,
            content=content,
            attachments=list(attachments or []),
            sequence=sequence,
        )
        messages.append(message)
        self._write(bot_id, "messages.json", [m.to_dict() for m in messages])
        return message

    def _all(self, bot_id: str) -> list[BotMessage]:
        data = self._read(bot_id, "messages.json", [])
        return [BotMessage.from_dict(item) for item in data if isinstance(item, dict)]

    def list(
        self, bot_id: str, conversation_id: str, *, after_sequence: int | None = None
    ) -> list[BotMessage]:
        messages = [m for m in self._all(bot_id) if m.conversation_id == conversation_id]
        messages.sort(key=lambda m: m.sequence)
        if after_sequence is not None:
            messages = [m for m in messages if m.sequence > after_sequence]
        return messages

    def get(self, bot_id: str, message_id: str) -> BotMessage | None:
        for message in self._all(bot_id):
            if message.message_id == message_id:
                require_same_bot(bot_id, message.bot_id, f"message:{message_id}")
                return message
        return None


class BotGoalStore(_BotDirStore):
    def create(
        self,
        bot_id: str,
        *,
        title: str,
        objective: str = "",
        constraints: list[str] | None = None,
        goal_id: str = "",
        plan_ref: str | None = None,
        memory_ref: str | None = None,
        cadence: str | None = None,
        trigger_ref: str | None = None,
    ) -> BotGoal:
        goals = self._all(bot_id)
        goal = BotGoal(
            goal_id=goal_id or _new_id("bgoal"),
            bot_id=bot_id,
            title=title,
            objective=objective,
            constraints=list(constraints or []),
            plan_ref=plan_ref,
            memory_ref=memory_ref,
            cadence=cadence,
            trigger_ref=trigger_ref,
        )
        goals.append(goal)
        self._write(bot_id, "goals.json", [g.to_dict() for g in goals])
        return goal

    def _all(self, bot_id: str) -> list[BotGoal]:
        data = self._read(bot_id, "goals.json", [])
        return [BotGoal.from_dict(item) for item in data if isinstance(item, dict)]

    def get(self, bot_id: str, goal_id: str) -> BotGoal | None:
        for goal in self._all(bot_id):
            if goal.goal_id == goal_id:
                return goal
        return None

    def require(self, bot_id: str, goal_id: str) -> BotGoal:
        goal = self.get(bot_id, goal_id)
        if goal is None:
            raise KeyError(f"bot goal {goal_id!r} not found for bot {bot_id!r}")
        require_same_bot(bot_id, goal.bot_id, f"bot_goal:{goal_id}")
        return goal

    def list(self, bot_id: str, *, status: str | None = None) -> list[BotGoal]:
        items = self._all(bot_id)
        if status is not None:
            items = [g for g in items if g.status == status]
        return items

    def update(
        self,
        bot_id: str,
        goal_id: str,
        *,
        status: str | None = None,
        add_goal_run_id: str | None = None,
    ) -> BotGoal:
        goals = self._all(bot_id)
        updated: BotGoal | None = None
        for goal in goals:
            if goal.goal_id != goal_id:
                continue
            require_same_bot(bot_id, goal.bot_id, f"bot_goal:{goal_id}")
            if status is not None:
                goal.status = status
            if add_goal_run_id and add_goal_run_id not in goal.goal_run_ids:
                goal.goal_run_ids.append(add_goal_run_id)
            goal.updated_at = _now_iso()
            updated = goal
            break
        if updated is None:
            raise KeyError(f"bot goal {goal_id!r} not found for bot {bot_id!r}")
        self._write(bot_id, "goals.json", [g.to_dict() for g in goals])
        return updated


class BotSessionStore(_BotDirStore):
    def create(
        self,
        bot_id: str,
        *,
        conversation_id: str,
        instance_id: str = "",
        session_id: str = "",
    ) -> BotSession:
        sessions = self._all(bot_id)
        session = BotSession(
            session_id=session_id or _new_id("bsess"),
            bot_id=bot_id,
            conversation_id=conversation_id,
            instance_id=instance_id,
        )
        sessions.append(session)
        self._write(bot_id, "sessions.json", [s.to_dict() for s in sessions])
        return session

    def _all(self, bot_id: str) -> list[BotSession]:
        data = self._read(bot_id, "sessions.json", [])
        return [BotSession.from_dict(item) for item in data if isinstance(item, dict)]

    def get(self, bot_id: str, session_id: str) -> BotSession | None:
        for session in self._all(bot_id):
            if session.session_id == session_id:
                require_same_bot(bot_id, session.bot_id, f"bot_session:{session_id}")
                return session
        return None

    def list(self, bot_id: str, *, conversation_id: str | None = None) -> list[BotSession]:
        items = self._all(bot_id)
        if conversation_id is not None:
            items = [s for s in items if s.conversation_id == conversation_id]
        return items


class BotProduct:
    """Facade over the B1 product stores for one bots root.

    The computer facade is a :class:`BotComputerProjection` over the sole
    computer authority (``PersistentComputerStore``); every other store here
    holds Bot identity/configuration only and no execution authority.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        computer_store: Any | None = None,
    ) -> None:
        base = Path(root) if root is not None else default_bots_root()
        self.root = base
        self.computer_store = (
            computer_store if computer_store is not None else default_computer_store(base)
        )
        self.computers = BotComputerProjection(self.computer_store)
        self.conversations = BotConversationStore(base)
        self.messages = BotMessageStore(base)
        self.goals = BotGoalStore(base)
        self.sessions = BotSessionStore(base)

    def status(self, bot_id: str, *, lifecycle_status: str = "") -> BotStatus:
        computer = self.computers.get(bot_id)
        goals = self.goals.list(bot_id, status=BotGoalStatus.ACTIVE)
        sessions = self.sessions.list(bot_id)
        conversations = self.conversations.list(bot_id)
        return BotStatus(
            bot_id=bot_id,
            lifecycle_status=lifecycle_status,
            computer_status=computer.status if computer else "",
            active_goal_count=len(goals),
            active_session_count=len([s for s in sessions if s.status == "active"]),
            conversation_count=len(conversations),
        )


__all__ = [
    "BotComputer",
    "BotComputerProjection",
    "BotComputerStatus",
    "BotConversation",
    "BotConversationStore",
    "BotGoal",
    "BotGoalStatus",
    "BotGoalStore",
    "BotMessage",
    "BotMessageStore",
    "BotProduct",
    "BotSession",
    "BotSessionStore",
    "BotStatus",
    "default_bots_root",
    "default_computer_store",
]
