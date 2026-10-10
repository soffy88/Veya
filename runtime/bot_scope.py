"""Bot scope primitives for the durable runtime layers (P3-A).

Single source of truth for the bot identity constant and the fail-closed
cross-bot refusal. Lives in ``runtime`` (not ``server``) so the durable
layers (computer / context / execution / verification) can enforce bot
isolation without depending on upper layers. It also owns the running-bot
binding (``bind_current_bot`` / ``current_bot``) so bot-scoped capabilities
read one execution identity. Bot configuration itself (``BotSpec`` /
``BotRegistry``) lives in :mod:`server.goal_run.bot_identity`, which
re-exports everything here.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

DEFAULT_BOT_ID = "veya-default"
"""Bot that owns every durable object created before P3-A (backward compatible)."""

# Running-bot binding for the current execution scope.  The GoalRun canonical
# worker binds the state's bot_id around the physical execution of each action
# (the same place ``require_same_bot`` already guards cross-bot actions), so a
# tool that is scoped to "the running bot" (e.g. per-bot Markdown memory) reads
# one identity without a new scheduler or a second identity store.
_current_bot: ContextVar[str | None] = ContextVar("veya_current_bot", default=None)


def bind_current_bot(bot_id: str | None) -> Token[str | None]:
    """Bind the running bot for the current execution scope."""
    return _current_bot.set(bot_id or None)


def reset_current_bot(token: Token[str | None]) -> None:
    """Restore the previous running-bot binding."""
    _current_bot.reset(token)


def current_bot() -> str | None:
    """Return the running bot id, or ``None`` when no bot scope is bound."""
    return _current_bot.get()


class CrossBotAccessDenied(RuntimeError):
    """A bot attempted to touch another bot's durable object.

    Raised fail-closed: no fallback, no partial read, no shared execution.
    """

    def __init__(self, resource: str, caller_bot_id: str, owner_bot_id: str):
        super().__init__(
            f"cross-bot access denied: {resource} belongs to bot "
            f"{owner_bot_id!r}, not {caller_bot_id!r}"
        )
        self.resource = resource
        self.caller_bot_id = caller_bot_id
        self.owner_bot_id = owner_bot_id


def require_same_bot(caller_bot_id: str, owner_bot_id: str, resource: str) -> None:
    """Refuse when ``caller`` is not the owning bot of ``resource``."""
    if caller_bot_id != owner_bot_id:
        raise CrossBotAccessDenied(resource, caller_bot_id, owner_bot_id)


__all__ = [
    "DEFAULT_BOT_ID",
    "CrossBotAccessDenied",
    "bind_current_bot",
    "current_bot",
    "require_same_bot",
    "reset_current_bot",
]
