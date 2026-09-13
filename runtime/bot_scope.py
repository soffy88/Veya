"""Bot scope primitives for the durable runtime layers (P3-A).

Single source of truth for the bot identity constant and the fail-closed
cross-bot refusal. Lives in ``runtime`` (not ``server``) so the durable
layers (computer / context / execution / verification) can enforce bot
isolation without depending on upper layers. Bot configuration itself
(``BotSpec`` / ``BotRegistry``) lives in
:mod:`server.goal_run.bot_identity`, which re-exports everything here.
"""

from __future__ import annotations

DEFAULT_BOT_ID = "veya-default"
"""Bot that owns every durable object created before P3-A (backward compatible)."""


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


__all__ = ["DEFAULT_BOT_ID", "CrossBotAccessDenied", "require_same_bot"]
