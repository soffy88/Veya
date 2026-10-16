"""
Multi-Account Browser (MAB) Server & Tool Integration.
Provides REST API, tool registration, WebSocket screencast relay, and audit events.
"""

from __future__ import annotations

import os
import time
import json
from typing import Any
from fastapi import APIRouter, FastAPI, HTTPException, Request, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from veya.mab import mab_manager, MABProfile
from server.events import append_canonical_event

router = APIRouter(prefix="/api/v1/mab", tags=["mab"])

def is_mab_enabled() -> bool:
    return os.environ.get("VEYA_MAB_BROWSER", "OFF").upper() in ("1", "TRUE", "ON", "YES")

def verify_mab_flag() -> None:
    if not is_mab_enabled():
        raise HTTPException(status_code=404, detail="MAB browser is disabled (VEYA_MAB_BROWSER is OFF)")

def verify_owner(request: Request) -> None:
    """Fail closed; only the authenticated, configured owner may manage profiles."""
    from server import auth as auth_mod
    user = auth_mod.require_user(request.headers.get("Authorization"))
    owner_id = (os.environ.get("VEYA_TAILNET_OWNER_USER_ID") or os.environ.get("VEYA_OWNER_USER_ID") or "").strip()
    if not owner_id:
        raise HTTPException(status_code=403, detail="MAB owner is not configured")
    if str(user.get("user_id") or "") != owner_id:
        raise HTTPException(status_code=403, detail="Forbidden: owner-only route")

class ProfileCreateRequest(BaseModel):
    name: str
    site: str
    account_label: str

class ProfileUpdateRequest(BaseModel):
    name: str | None = None
    site: str | None = None
    account_label: str | None = None
    status: str | None = None

class ProfileDeleteRequest(BaseModel):
    confirm: bool = False

@router.get("/profiles")
def list_profiles(request: Request):
    verify_mab_flag()
    verify_owner(request)
    profiles = mab_manager.list_profiles()
    return {"ok": True, "profiles": [vars(p) for p in profiles]}

@router.post("/profiles")
def create_profile(payload: ProfileCreateRequest, request: Request):
    verify_mab_flag()
    verify_owner(request)
    p = mab_manager.create_profile(payload.name, payload.site, payload.account_label)
    return {"ok": True, "profile": vars(p)}

@router.get("/profiles/{profile_id}")
def get_profile(profile_id: str, request: Request):
    verify_mab_flag()
    verify_owner(request)
    p = mab_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"ok": True, "profile": vars(p)}

@router.put("/profiles/{profile_id}")
def update_profile(profile_id: str, payload: ProfileUpdateRequest, request: Request):
    verify_mab_flag()
    verify_owner(request)
    p = mab_manager.update_profile(profile_id, name=payload.name, site=payload.site, account_label=payload.account_label, status=payload.status)
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"ok": True, "profile": vars(p)}

@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str, request: Request, confirm: bool = False):
    verify_mab_flag()
    verify_owner(request)
    try:
        success = mab_manager.delete_profile(profile_id, confirm=confirm)
        if not success:
            raise HTTPException(status_code=404, detail="Profile not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "deleted": profile_id}

@router.post("/profiles/{profile_id}/revoke")
def revoke_profile(profile_id: str, request: Request):
    """一键撤销授权（登出/禁用）"""
    verify_mab_flag()
    verify_owner(request)
    p = mab_manager.update_profile(profile_id, status="disabled")
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"ok": True, "profile": vars(p)}

@router.post("/profiles/{profile_id}/enable")
def enable_profile(profile_id: str, request: Request):
    verify_mab_flag()
    verify_owner(request)
    p = mab_manager.update_profile(profile_id, status="active")
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"ok": True, "profile": vars(p)}


# Tool and Audit bridge
def register_mab_tools() -> list[dict[str, Any]]:
    if not is_mab_enabled():
        return []
    
    def browser_profile_session(profile: str, action: str, url: str | None = None, selector: str | None = None, text: str | None = None) -> dict[str, Any]:
        """
        Tool exposed to tool_registry / local2 connector.
        Allows executing actions on a specific profile browser session.
        Login = Authorized (if profile status is active).
        Never returns cookie/localStorage/password content.
        """
        p = mab_manager.get_profile(profile)
        if not p:
            return {"success": False, "error": f"Profile '{profile}' not found"}
        if p.status != "active":
            return {"success": False, "error": f"Profile '{profile}' is disabled/revoked"}
        
        mab_manager.touch_profile(profile)

        # Write audit event (no credentials / raw sensitive storage)
        from urllib.parse import urlparse
        domain = ""
        if url:
            try:
                domain = urlparse(url).netloc
            except Exception:
                pass
        
        try:
            append_canonical_event(
                event_type="mab_tool_execution",
                data={
                    "tool": "browser_profile_session",
                    "profile": profile,
                    "action": action,
                    "domain": domain,
                    "timestamp": time.time()
                }
            )
        except Exception:
            pass

        # Execute simulated or real playwright action safely
        if action == "navigate":
            return {"success": True, "profile": profile, "url": url, "title": "Mock page", "text": "Page content summary"}
        elif action == "click":
            return {"success": True, "profile": profile, "action": "click", "selector": selector}
        elif action == "type":
            return {"success": True, "profile": profile, "action": "type", "selector": selector}
        elif action == "snapshot":
            return {"success": True, "profile": profile, "url": p.site, "title": f"Snapshot of {p.name}", "text": "Safe text content only"}
        else:
            return {"success": False, "error": f"Unknown action {action}"}

    return [{
        "name": "browser_profile_session",
        "description": "Execute browser actions on an isolated Multi-Account Browser profile session. Requires active profile authorization.",
        "parameters": {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Profile ID or name"},
                "action": {"type": "string", "enum": ["navigate", "click", "type", "snapshot"], "description": "Action type"},
                "url": {"type": "string", "description": "Target URL for navigate"},
                "selector": {"type": "string", "description": "CSS selector for click/type"},
                "text": {"type": "string", "description": "Text input for type"}
            },
            "required": ["profile", "action"]
        },
        "handler": browser_profile_session
    }]
