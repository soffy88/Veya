"""Event hub routes: ingest + read-only rules/recent + reload.

The ingest route is the third hub entry point (external cron/scripts).
It is owner-authed like the bots API. Hub outcomes are always HTTP 200
with the hub status in the body (duplicate/dropped are normal,
idempotent outcomes); only a malformed envelope is 422.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from server import auth as auth_mod
from server.nano_w10.event_hub import get_event_hub

_log = logging.getLogger("veya.routes.events")

router = APIRouter(prefix="/api/v1/events", tags=["events"])


@router.post("/ingest")
async def ingest_event(
    request: Request,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Accept one pointer-only envelope and route it through the hub."""

    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"body must be a JSON object: {type(exc).__name__}",
        ) from exc
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="body must be a JSON object",
        )
    hub = get_event_hub()
    # Envelope shape is validated at the boundary regardless of the flag:
    # a malformed envelope is a client error even while routing is off.
    from server.nano_w10.event_hub import validate_envelope

    try:
        validate_envelope(data)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    result = await hub.ingest(data, user=dict(user))
    if result.get("status") == "refused":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(result.get("reason") or "envelope refused"),
        )
    if result.get("status") == "unavailable":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(result.get("reason") or "event hub unavailable"),
        )
    return result


@router.get("/rules")
async def list_rules(
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Current effective rules (owner-only; rules are owner-authored)."""

    hub = get_event_hub()
    return {
        "rules": hub.rules_snapshot(),
        "error": hub.rules_error,
        "enabled": hub._enabled_fn(),
    }


@router.post("/rules/reload")
async def reload_rules(
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Reload rules from disk. A bad file keeps the previous rules (422)."""

    result = get_event_hub().reload_rules()
    if not result["reloaded"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(result["error"] or "rules reload failed"),
        )
    return result


@router.get("/recent")
async def recent_events(
    user: dict[str, Any] = Depends(auth_mod.require_user),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Recent hub events (canonical event log, topic prefix ``event.``)."""

    from server.events import EventStore

    events = EventStore().read_all(topic_prefixes=("event.",))
    return {"events": events[-limit:], "total": len(events)}
