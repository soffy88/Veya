"""Per-bot conversation store (Lorca P1).

One JSON file per bot under ``~/.veya/bots/<bot_id>/`` (override with
``VEYA_BOTS_ROOT``). Every write is atomic (tmp file + ``os.replace``) so a
crash mid-write can never truncate a conversation, and a corrupt file degrades
to "no messages" instead of breaking the bot.

This is a projection of what the bots said. It is not a task store, not an
execution authority and not a second history: the durable execution record of
a turn stays in GoalRun, and the Session/Task projections stay in the existing
product stores.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

#: Reply a group member uses to say "I have nothing to add this round".
PASS_SENTINEL = "[PASS]"

#: A bot id becomes a directory name, so it is restricted to a safe alphabet.
_BOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_MESSAGES_FILE = "messages.json"


def default_bots_root() -> Path:
    """Root of every bot's own directory (``VEYA_BOTS_ROOT`` overrides it)."""

    raw = os.environ.get("VEYA_BOTS_ROOT") or str(Path.home() / ".veya" / "bots")
    return Path(raw).expanduser()


def safe_bot_id(bot_id: str) -> str:
    """Return ``bot_id`` if it is safe as a directory name, else raise."""

    value = str(bot_id or "")
    if not _BOT_ID_RE.match(value):
        raise ValueError(f"invalid bot_id: {bot_id!r}")
    return value


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write ``payload`` as JSON, replacing ``path`` atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class BotMessage(dict):
    """One recorded message: ``{id, role, bot_id, text, task_id, ts, hidden}``."""


class ConversationStore:
    """Read/append the message log of each bot in its own directory."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else default_bots_root()

    def bot_dir(self, bot_id: str) -> Path:
        return self.root / safe_bot_id(bot_id)

    def _path(self, bot_id: str) -> Path:
        return self.bot_dir(bot_id) / _MESSAGES_FILE

    def list(self, bot_id: str, *, include_hidden: bool = False) -> list[BotMessage]:
        """Messages of one bot, oldest first.

        Hidden messages (a PASS reply, or an internal note) are only returned
        to callers that explicitly ask for them.
        """

        try:
            raw = json.loads(self._path(bot_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        messages = [BotMessage(item) for item in raw if isinstance(item, dict)]
        if include_hidden:
            return messages
        return [item for item in messages if not item.get("hidden")]

    def append(
        self,
        bot_id: str,
        *,
        role: str,
        text: str,
        task_id: str | None = None,
        hidden: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> BotMessage:
        """Append one message to ``bot_id``'s log and return it."""

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
        # The PASS sentinel is an internal control reply: it is stored, never
        # shown. This is the single place that rule is applied.
        message = BotMessage(
            {
                "id": f"msg_{uuid.uuid4().hex[:16]}",
                "role": role,
                "bot_id": safe_bot_id(bot_id),
                "text": body,
                "task_id": task_id,
                "ts": time.time(),
                "hidden": bool(hidden) or body.strip() == PASS_SENTINEL,
                # Optional structured marker (NANO-W10 W5 finish checkpoint).
                # Absent on every message written before this, so an old log
                # reads exactly as it did.
                "meta": dict(meta) if meta else None,
            }
        )
        messages = self.list(bot_id, include_hidden=True)
        messages.append(message)
        atomic_write_json(self._path(bot_id), messages)
        return message

    def clear(self, bot_id: str) -> None:
        """Forget one bot's messages (used when a bot is deleted)."""

        with contextlib.suppress(OSError):
            self._path(bot_id).unlink()


def conversation_store(root: str | Path | None = None) -> ConversationStore:
    """Store bound to ``root`` or the process default root."""

    return ConversationStore(root)


__all__ = [
    "PASS_SENTINEL",
    "BotMessage",
    "ConversationStore",
    "atomic_write_json",
    "conversation_store",
    "default_bots_root",
    "safe_bot_id",
]
