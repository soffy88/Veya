"""Owner-only management API for the opt-in Tailnet channel."""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.tailnet_channel import enabled, get_service, query_request_status

router = APIRouter(prefix="/api/v1/tailnet", tags=["tailnet-channel"])


def require_tailnet_owner(user: dict[str, Any] = Depends(auth_mod.require_user)) -> dict[str, Any]:
    """Tailnet policy management is security-sensitive and fails closed."""
    user_id = str(user.get("user_id") or "")
    configured_owner = (os.getenv("VEYA_TAILNET_OWNER_USER_ID") or os.getenv("VEYA_OWNER_USER_ID") or "").strip()
    roles = {str(role).lower() for role in user.get("roles", [])} if isinstance(user.get("roles", []), (list, tuple, set)) else set()
    role = str(user.get("role") or "").lower()
    allowed = bool(configured_owner and user_id == configured_owner) or bool(
        user.get("is_owner") is True or user.get("is_admin") is True or role in {"owner", "admin"} or roles.intersection({"owner", "admin"})
    )
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tailnet management is owner-only; configure VEYA_TAILNET_OWNER_USER_ID")
    return user


class PolicyUpdate(BaseModel):
    mode: str = Field("allowlist", pattern="^(allowlist|tailnet)$")
    allow: list[str] = Field(default_factory=list, max_length=500)
    deny: list[str] = Field(default_factory=list, max_length=500)
    bindings: dict[str, dict[str, str]] = Field(default_factory=dict, max_length=500)
    wake_policy: dict[str, Any] = Field(default_factory=lambda: {"default_max": "batch", "peers": {}})
    quiet_hours: dict[str, str] | None = None
    wake_rate: int = Field(default=6, ge=0, le=1000)
    agents: dict[str, dict[str, str]] = Field(default_factory=dict, max_length=500)


class PeerUpdate(BaseModel):
    allow: list[str] = Field(default_factory=list, max_length=500)
    deny: list[str] = Field(default_factory=list, max_length=500)


def _validate_owned_targets(payload: PolicyUpdate, user: dict[str, Any]) -> None:
    """Reuse existing owner checks; remote peers cannot select somebody else's bot."""
    from server.bot_team.groups import GroupStore
    from server.mcp_presets import _owned_bot

    group_store = GroupStore()
    targets = list(payload.bindings.values()) + list(payload.agents.values())
    for binding in targets:
        target_type = binding.get("target_type")
        target_id = binding.get("target_id")
        if not target_type and not target_id:
            continue
        if target_type == "bot" and target_id:
            _owned_bot(target_id, user)
        elif target_type == "group" and target_id:
            group = group_store.get(target_id)
            if group is None:
                raise HTTPException(status_code=404, detail="group not found")
            for bot_id in group.bot_ids:
                _owned_bot(bot_id, user)
        else:
            raise HTTPException(status_code=400, detail="target must declare bot/group and target_id")


@router.get("/status")
async def tailnet_status(_: dict[str, Any] = Depends(require_tailnet_owner)) -> dict[str, Any]:
    service = get_service()
    policy = service.load_policy()
    return {
        "enabled": enabled(),
        "listening": bool(service.server and service.thread and service.thread.is_alive()),
        "port": service.port,
        "default_port": 7767,
        "peer_default_port": service.peer_port,
        "node": service.self_node() or None,
        "policy_mode": policy["mode"],
        "allow_count": len(policy["allow"]),
        "deny_count": len(policy["deny"]),
        "capabilities": ["request-lifecycle-v1"],
    }


@router.get("/policy")
async def get_policy(_: dict[str, Any] = Depends(require_tailnet_owner)) -> dict[str, Any]:
    return get_service().load_policy()


@router.put("/policy")
async def put_policy(payload: PolicyUpdate, user: dict[str, Any] = Depends(require_tailnet_owner)) -> dict[str, Any]:
    try:
        _validate_owned_targets(payload, user)
        policy = get_service().save_policy(payload.model_dump())
    except HTTPException:
        raise
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "policy": policy}


@router.get("/peers")
async def get_peers(_: dict[str, Any] = Depends(require_tailnet_owner)) -> dict[str, Any]:
    service = get_service()
    policy = service.load_policy()
    return {"peers": service.list_peers(), "allow": policy["allow"], "deny": policy["deny"], "mode": policy["mode"]}


@router.put("/peers")
async def put_peers(payload: PeerUpdate, _: dict[str, Any] = Depends(require_tailnet_owner)) -> dict[str, Any]:
    service = get_service()
    policy = service.load_policy()
    policy.update({"allow": payload.allow, "deny": payload.deny})
    try:
        saved = service.save_policy(policy)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "allow": saved["allow"], "deny": saved["deny"], "mode": saved["mode"]}


@router.get("/requests/{request_id}")
async def get_request(request_id: str, peer: str | None = Query(default=None, max_length=300), _: dict[str, Any] = Depends(require_tailnet_owner)) -> dict[str, Any]:
    service = get_service()
    if peer:
        try:
            return query_request_status(peer, request_id)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    result = service.request_status(request_id)
    if not result:
        raise HTTPException(status_code=404, detail="tracked request not found")
    return {"ok": True, "request": result}
