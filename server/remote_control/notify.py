"""W13 notifications — through the existing push path, nowhere else.

``global_notifier.push(...)`` is the repository's one notification fan-out, and
``WEB_PUSH_FLAG`` is what makes a message leave a closed browser tab
(``server/bot_team/push.py:550-568``). W12's supervisor reports omit that flag
and therefore never reach a device; a cross-device feature cannot repeat the
mistake, so both W13 events set it.

Two events only:

* ``done``            → ``SUCCESS``          / ``kind=remote_done``
* ``needs approval``  → ``HITL_REQUIRED``    / ``kind=remote_approval``

``user_id`` is always the host owner, so a notification is per-user and is
never broadcast.
"""

from __future__ import annotations

from typing import Any

KIND_DONE = "remote_done"
KIND_APPROVAL = "remote_approval"


def _deep_link(session_id: str) -> str:
    return f"/remote?session={session_id}"


def notify_done(
    *,
    owner_id: str,
    session_id: str,
    agent_label: str,
    host_id: str,
    summary: str = "",
) -> str:
    """Tell the owner a remote agent finished, with verifier evidence attached."""

    from server.notification_center import WEB_PUSH_FLAG, global_notifier

    content = summary.strip() or f"{agent_label} on {host_id} finished"
    return global_notifier.push(
        "SUCCESS",
        f"远程任务完成：{agent_label}",
        content[:500],
        payload={
            "url": _deep_link(session_id),
            "tag": f"remote:{session_id}",
            "kind": KIND_DONE,
            "session_id": session_id,
            "host_id": host_id,
            "agent_label": agent_label,
            WEB_PUSH_FLAG: True,
        },
        user_id=owner_id,
    )


def notify_needs_approval(
    *,
    owner_id: str,
    session_id: str,
    host_id: str,
    agent_label: str,
    action: str,
    approval_id: str = "",
) -> str:
    """Tell the owner a remote mutation is waiting on them."""

    from server.notification_center import WEB_PUSH_FLAG, global_notifier

    return global_notifier.push(
        "HITL_REQUIRED",
        f"远程任务待批准：{host_id}",
        f"{action} on {host_id} ({agent_label}) needs your approval",
        payload={
            "url": f"/approvals/{approval_id}" if approval_id else _deep_link(session_id),
            "tag": f"remote-approval:{session_id}",
            "kind": KIND_APPROVAL,
            "session_id": session_id,
            "host_id": host_id,
            "agent_label": agent_label,
            "action": action,
            "approval_id": approval_id,
            WEB_PUSH_FLAG: True,
        },
        user_id=owner_id,
    )


def notify_cancelled(*, owner_id: str, session_id: str, host_id: str, reason: str = "") -> str:
    """Tell the owner a remote run was cancelled."""

    from server.notification_center import WEB_PUSH_FLAG, global_notifier

    return global_notifier.push(
        "INFO",
        f"远程任务已取消：{host_id}",
        reason[:300] or "cancelled",
        payload={
            "url": _deep_link(session_id),
            "tag": f"remote:{session_id}",
            "kind": KIND_DONE,
            "session_id": session_id,
            "host_id": host_id,
            "cancelled": True,
            WEB_PUSH_FLAG: True,
        },
        user_id=owner_id,
    )


def pending_approval_cards(owner_id: str) -> list[dict[str, Any]]:
    """The approve/deny cards for one owner, in the shape the phone renders."""

    from server.remote_control import approval as approval_mod

    cards: list[dict[str, Any]] = []
    for record in approval_mod.pending_for_scope(owner_id):
        args = record.get("tool_args") or {}
        approval_id = str(record.get("request_id") or "")
        cards.append(
            {
                "approval_id": approval_id,
                "tool": str(record.get("tool") or ""),
                "session_id": str(args.get("session_id") or ""),
                "host_id": str(args.get("host_id") or ""),
                "reason": str(record.get("reason") or ""),
                "created_at": record.get("created_at"),
                "resolve_url": f"/api/v1/approvals/{approval_id}/resolve",
            }
        )
    return cards


__all__ = [
    "KIND_APPROVAL",
    "KIND_DONE",
    "notify_cancelled",
    "notify_done",
    "notify_needs_approval",
    "pending_approval_cards",
]
