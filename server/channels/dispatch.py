"""server/channels/dispatch.py — Inbound channel message dispatch (Layer 4).

Maps an external chat to a Veya target and hands the message to the existing
bot/group chat path (P1 bots, P2 groups). No new execution authority is
introduced: this module only *routes* into ``run_bot_turn`` /
``run_group_turn``, exactly like ``server/routes/bot_team.py`` does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from veya.obase.channels import ChannelMessage
from veya.oskill.im.pseudo import anonymize_user_id

from .base import ChannelAdapter

_log = logging.getLogger(__name__)


@dataclass
class DispatchOutcome:
    """What happened to one inbound message."""

    ok: bool
    status: str
    user_ref: str = ""
    target_type: str = ""
    target_id: str = ""
    reply: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "user_ref": self.user_ref,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "detail": self.detail,
        }


def anonymous_ref(platform: str, external_user_id: str) -> str:
    """Stable non-PII reference for an external platform user (SPEC §5.5.1)."""
    return anonymize_user_id(f"{platform}:{external_user_id}")


async def dispatch_inbound(
    message: ChannelMessage,
    *,
    binding: dict[str, Any] | None,
    adapter: ChannelAdapter,
    send: bool = True,
) -> DispatchOutcome:
    """Route an inbound message to its bound bot/group and reply.

    ``send=False`` performs routing and execution without posting the reply
    (used when the reply is delivered out of band).
    """
    if not binding:
        return DispatchOutcome(ok=True, status="unbound", detail="no binding for this chat")

    owner_ref = str(binding.get("owner_ref") or "")
    target_type = str(binding.get("target_type") or "")
    target_id = str(binding.get("target_id") or "")
    user_ref = owner_ref or anonymous_ref(message.platform, message.external_user_id)
    user = {"user_id": user_ref, "username": user_ref}

    if target_type == "bot":
        reply = await _run_bot(target_id, message.text, user)
    elif target_type == "group":
        reply = await _run_group(target_id, message.text, user)
    else:
        return DispatchOutcome(
            ok=False,
            status="error",
            user_ref=user_ref,
            detail=f"unknown target_type: {target_type}",
        )

    if send and reply:
        await _deliver(adapter, message.external_chat_id, reply)

    return DispatchOutcome(
        ok=True,
        status="answered",
        user_ref=user_ref,
        target_type=target_type,
        target_id=target_id,
        reply=reply,
    )


async def _run_bot(bot_id: str, text: str, user: dict[str, Any]) -> str:
    """Hand the message to the existing single-bot turn path (P1)."""
    from server.bot_team.conversations import ConversationStore
    from server.bot_team.turns import run_bot_turn
    from server.goal_run.bot_identity import BotRegistry

    spec = BotRegistry().get(bot_id)
    if spec is None:
        _log.warning("Channel bound to unknown bot %s", bot_id)
        return ""
    turn = await run_bot_turn(
        bot=spec,
        text=text,
        user=dict(user),
        conversations=ConversationStore(),
    )
    return getattr(turn, "reply", "") or ""


async def _run_group(group_id: str, text: str, user: dict[str, Any]) -> str:
    """Hand the message to the existing group chat path (P2)."""
    from server.bot_team.groups import GroupStore, run_group_turn
    from server.goal_run.bot_identity import BotRegistry

    group = GroupStore().get(group_id)
    if group is None:
        _log.warning("Channel bound to unknown group %s", group_id)
        return ""
    result = await run_group_turn(
        group=group,
        text=text,
        user=dict(user),
        registry=BotRegistry(),
    )
    replies = result.get("replies") or []
    lines = [str(r.get("reply", "")).strip() for r in replies if r.get("reply")]
    return "\n\n".join(line for line in lines if line)


async def _deliver(adapter: ChannelAdapter, external_chat_id: str, reply: str) -> None:
    """Post the reply back through the platform client (chunking long ones)."""
    from .clients import client_for

    try:
        client = await client_for(adapter.platform)
    except Exception:
        _log.warning("No client configured for platform %s", adapter.platform, exc_info=True)
        return
    try:
        await adapter.send_reply(client, external_chat_id=external_chat_id, text=reply)
    except Exception:
        _log.warning("Failed to post reply to %s", adapter.platform, exc_info=True)


__all__ = ["DispatchOutcome", "anonymous_ref", "dispatch_inbound"]
