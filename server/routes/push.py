"""veya push endpoints (Lorca P5): VAPID public key + per-user subscriptions.

``/api/v1/push`` is the browser-facing half of the bot team's Web Push. It
hands out the public key the browser needs to subscribe, keeps one record per
subscription per user, and installs the delivery hook
(``server/bot_team/push.py``) on the notification centre while this module is
imported — so importing the router is all ``server/app.py`` has to do.

No scheduler, no task store and no executor: a push is a projection of a turn
that already ran, and the durable record of that turn stays in GoalRun.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.bot_team.push import (
    PushNotifier,
    SubscriptionStore,
    install_push_hook,
)
from server.bot_team.push import (
    subscription_id as subscription_id_for,
)

router = APIRouter(prefix="/api/v1/push", tags=["push"])


class SubscriptionCreateRequest(BaseModel):
    endpoint: str = Field(..., min_length=1, max_length=2000)
    keys: dict[str, str] = Field(..., min_length=2, max_length=2)
    expiration_time: int | None = Field(default=None, ge=0)


def _store() -> SubscriptionStore:
    return SubscriptionStore()


def _notifier() -> PushNotifier:
    """A notifier bound to the default push root."""

    from server.bot_team.push import get_push_notifier

    return get_push_notifier()


# Importing this module is what puts the hook on the notification centre: no
# bot-turn code has to know that pushes exist.
install_push_hook()


@router.get("/public_key")
async def public_key() -> dict[str, Any]:
    """The VAPID public key the browser signs its subscription with."""

    vapid = _notifier().vapid.load_or_create()
    return {"public_key": vapid.public_key, **vapid.to_public_dict()}


@router.get("/status")
async def status() -> dict[str, Any]:
    """Whether this deployment can push at all, and how many subscriptions exist."""

    return _notifier().describe()


@router.get("/subscriptions")
async def list_subscriptions(
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """This user's subscriptions (a phone and a laptop are two records)."""

    return {
        "user_id": user["user_id"],
        "subscriptions": [_view(item) for item in _store().list(user["user_id"])],
    }


@router.post("/subscriptions", status_code=201)
async def subscribe(
    req: SubscriptionCreateRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Register (or refresh) one push subscription for this user."""

    p256dh = str(req.keys.get("p256dh") or "")
    auth = str(req.keys.get("auth") or "")
    try:
        record = _store().upsert(
            endpoint=req.endpoint,
            p256dh=p256dh,
            auth=auth,
            user_id=str(user["user_id"]),
            expiration_time=req.expiration_time,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "subscribed", "subscription": _view(record)}


def _view(record: Any) -> dict[str, Any]:
    """What a subscription looks like over the wire (no raw keys echoed back)."""

    parsed = urlparse(record.endpoint)
    return {
        "id": record.id,
        "endpoint_host": parsed.hostname or "",
        "user_id": record.user_id,
        "created_at": record.created_at,
        "expiration_time": record.expiration_time,
    }


@router.delete("/subscriptions/{subscription_id}")
async def unsubscribe(
    subscription_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Drop one of this user's subscriptions."""

    if not _store().delete(subscription_id, user_id=str(user["user_id"])):
        raise HTTPException(status_code=404, detail=f"subscription {subscription_id!r} not found")
    return {"status": "unsubscribed", "id": subscription_id}


@router.delete("/subscriptions")
async def unsubscribe_by_endpoint(
    endpoint: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Drop a subscription by its endpoint (the browser often only has that)."""

    record_id = subscription_id_for(endpoint)
    if not _store().delete(record_id, user_id=str(user["user_id"])):
        raise HTTPException(status_code=404, detail="subscription for that endpoint not found")
    return {"status": "unsubscribed", "id": record_id}


__all__ = ["router"]
