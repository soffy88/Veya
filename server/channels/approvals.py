"""server/channels/approvals.py — Approval delivery seam for channels (Layer 4).

Spec item 2: approvals that arrive while the user is on a channel. W6 (the
approvals surface) does not exist yet, so this is deliberately only a hook:
no dependency, no second approval system, and nothing here decides whether
an action is approved.

Approval decisions belong to ``SideEffectLedger`` + ``ActionGateway``
(``server/action_gateway_adapter.py``) + GoalRun state — the frozen Veya
decision in 3O_RECONCILIATION OBSOLETE. This module only *renders* an
already-decided approval and pushes it to whatever chats the owner bound.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from veya.oskill.im.telegram import format_telegram_message

_log = logging.getLogger(__name__)


class ApprovalChannel:
    """Registry of approval renderers, keyed by channel platform."""

    def __init__(self) -> None:
        self._renderers: dict[str, Callable[[dict[str, Any]], list[Any]]] = {}

    def register(self, platform: str, renderer: Callable[[dict[str, Any]], list[Any]]) -> None:
        """Register how an approval payload renders on ``platform``."""
        self._renderers[platform] = renderer

    def supports(self, platform: str) -> bool:
        return platform in self._renderers

    def render(self, platform: str, approval: dict[str, Any]) -> list[Any]:
        """Render ``approval`` for ``platform``.

        Raises KeyError for an unregistered platform; callers should check
        ``supports`` first.
        """
        return self._renderers[platform](approval)

    async def deliver(
        self, *, platform: str, owner_ref: str, approval: dict[str, Any]
    ) -> dict[str, Any]:
        """Push an already-decided approval to every chat the owner bound."""
        if not self.supports(platform):
            return {"ok": False, "reason": f"no approval renderer for {platform}"}

        from server.channels import adapter_for, binding_store
        from server.channels.clients import client_for

        try:
            client = await client_for(platform)
        except Exception:
            _log.warning("no client for approval delivery on %s", platform, exc_info=True)
            return {"ok": False, "reason": "client unavailable"}

        payloads = self.render(platform, approval)
        delivered = 0
        for binding in binding_store().list(owner_ref=owner_ref):
            if binding.get("platform") != platform:
                continue
            try:
                await adapter_for(platform).send_reply(
                    client,
                    external_chat_id=str(binding.get("external_chat_id", "")),
                    text="\n\n".join(str(p) for p in payloads),
                )
                delivered += 1
            except Exception:
                _log.warning("approval delivery failed on %s", platform, exc_info=True)
        return {"ok": delivered > 0, "delivered": delivered}


def _default_render(approval: dict[str, Any]) -> list[Any]:
    title = str(approval.get("title") or "Approval required")
    detail = str(approval.get("detail") or "")
    action_id = str(approval.get("action_id") or "")
    status = str(approval.get("status") or "pending")
    text = f"🔔 *{title}*\n\n{detail}\n\nstatus: {status}"
    if action_id:
        text += f"\nid: {action_id}"
    return format_telegram_message(text)


_channel = ApprovalChannel()
_channel.register("telegram", _default_render)


def approval_channel() -> ApprovalChannel:
    """Process-wide approval channel registry."""
    return _channel


def register_approval_handler(
    platform: str,
    renderer: Callable[[dict[str, Any]], list[Any]],
) -> None:
    """Register an approval renderer for ``platform`` (W6 integration point)."""
    _channel.register(platform, renderer)


__all__ = ["ApprovalChannel", "approval_channel", "register_approval_handler"]
