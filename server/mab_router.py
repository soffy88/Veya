"""
Multi-Account Browser (MAB) Server & Tool Integration.
Provides REST API, tool registration, WebSocket screencast relay, and audit events.
"""

from __future__ import annotations

import asyncio
import os
import time
import json
import secrets
import threading
from typing import Any
from fastapi import APIRouter, FastAPI, HTTPException, Request, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from veya.mab import mab_manager, MABProfile
from server.events import append_canonical_event

from server.auth import require_owner

router = APIRouter(prefix="/api/v1/mab", tags=["mab"], dependencies=[Depends(require_owner)])
_WS_TICKETS: dict[str, dict[str, Any]] = {}
_WS_TICKETS_LOCK = threading.Lock()
_WS_TICKET_TTL = 60

def is_mab_enabled() -> bool:
    return os.environ.get("VEYA_MAB_BROWSER", "OFF").upper() in ("1", "TRUE", "ON", "YES")

def verify_mab_flag() -> None:
    if not is_mab_enabled():
        raise HTTPException(status_code=404, detail="MAB browser is disabled (VEYA_MAB_BROWSER is OFF)")

def verify_owner(request: Request) -> dict[str, Any]:
    """Use canonical require_owner from server.auth."""
    from server.auth import require_owner
    auth_header = request.headers.get("Authorization")
    return require_owner(authorization=auth_header, request=request)

class ProfileCreateRequest(BaseModel):
    name: str
    site: str
    account_label: str

class ProfileUpdateRequest(BaseModel):
    name: str | None = None
    site: str | None = None
    account_label: str | None = None
    status: str | None = None

def verify_mab_flag_check() -> None:
    if not is_mab_enabled():
        raise HTTPException(status_code=404, detail="MAB browser is disabled (VEYA_MAB_BROWSER is OFF)")

def verify_mab_flag(_flag: None = Depends(verify_mab_flag_check)) -> None:
    pass

@router.get("/profiles", dependencies=[Depends(require_owner), Depends(verify_mab_flag_check)])
def list_profiles(request: Request):
    profiles = mab_manager.list_profiles()
    return {"ok": True, "profiles": [vars(p) for p in profiles]}

@router.post("/profiles", dependencies=[Depends(require_owner), Depends(verify_mab_flag_check)])
def create_profile(payload: ProfileCreateRequest, request: Request):
    p = mab_manager.create_profile(payload.name, payload.site, payload.account_label)
    return {"ok": True, "profile": vars(p)}

@router.get("/profiles/{profile_id}", dependencies=[Depends(require_owner), Depends(verify_mab_flag_check)])
def get_profile(profile_id: str, request: Request):
    p = mab_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"ok": True, "profile": vars(p)}

@router.put("/profiles/{profile_id}", dependencies=[Depends(require_owner), Depends(verify_mab_flag_check)])
def update_profile(profile_id: str, payload: ProfileUpdateRequest, request: Request):
    p = mab_manager.update_profile(profile_id, name=payload.name, site=payload.site, account_label=payload.account_label, status=payload.status)
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"ok": True, "profile": vars(p)}

@router.delete("/profiles/{profile_id}", dependencies=[Depends(require_owner), Depends(verify_mab_flag_check)])
def delete_profile(profile_id: str, request: Request, confirm: bool = False):
    try:
        success = mab_manager.delete_profile(profile_id, confirm=confirm)
        if not success:
            raise HTTPException(status_code=404, detail="Profile not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "deleted": profile_id}

@router.post("/profiles/{profile_id}/revoke", dependencies=[Depends(require_owner), Depends(verify_mab_flag_check)])
def revoke_profile(profile_id: str, request: Request):
    """一键撤销授权（登出/禁用）"""
    p = mab_manager.update_profile(profile_id, status="disabled")
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"ok": True, "profile": vars(p)}

@router.post("/profiles/{profile_id}/enable", dependencies=[Depends(require_owner), Depends(verify_mab_flag_check)])
def enable_profile(profile_id: str, request: Request):
    p = mab_manager.update_profile(profile_id, status="active")
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"ok": True, "profile": vars(p)}


@router.post("/profiles/{profile_id}/stream-ticket", dependencies=[Depends(require_owner), Depends(verify_mab_flag_check)])
def create_stream_ticket(profile_id: str, request: Request, current_user: dict[str, Any] = Depends(require_owner)):
    p = mab_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    if p.status != "active":
        raise HTTPException(status_code=403, detail="Profile not active")
    token = secrets.token_urlsafe(32)
    user_id = current_user.get("user_id") or current_user.get("sub") or "owner"
    with _WS_TICKETS_LOCK:
        _WS_TICKETS[token] = {
            "profile_id": profile_id,
            "user_id": user_id,
            "expires": time.monotonic() + _WS_TICKET_TTL
        }
    return {"ok": True, "ticket": token, "expires_in": _WS_TICKET_TTL}
@router.websocket("/profiles/{profile_id}/stream")
async def profile_stream_websocket(websocket: WebSocket, profile_id: str):
    if not is_mab_enabled():
        await websocket.close(code=4404, reason="MAB browser is disabled")
        return
    user_id, err_code = _take_ws_ticket(websocket, profile_id)
    if err_code != 0:
        await websocket.close(code=err_code, reason="Unauthorized or Forbidden")
        return
    profile = mab_manager.get_profile(profile_id)
    if not profile or profile.status != "active":
        await websocket.close(code=4403, reason="Profile not active")
        return
    await websocket.accept()

    session = None
    cdp = None
    screencast_task = None
    try:
        session = await mab_manager.ensure_browser(profile_id)
        page = session.get("page")
        context = session.get("context")
        if page and context:
            cdp = await context.new_cdp_session(page)
            await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 80, "everyNthFrame": 1})

            async def screencast_loop():
                try:
                    while True:
                        event = await cdp.wait_for_event("Page.screencastFrame", timeout=30.0)
                        if not event:
                            break
                        await _forward_cdp_frame(websocket, cdp, event)
                except Exception:
                    pass

            screencast_task = asyncio.create_task(screencast_loop())

        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except Exception:
                continue
            msg_type = msg.get("type")
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif msg_type in {"mouse", "keyboard", "text"} and cdp:
                try:
                    await _dispatch_cdp_input(cdp, msg)
                except Exception as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
    except WebSocketDisconnect:
        pass
    finally:
        if screencast_task and not screencast_task.done():
            screencast_task.cancel()
        if cdp:
            try:
                await cdp.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                await cdp.detach()
            except Exception:
                pass



def _take_ws_ticket(websocket: WebSocket, profile_id: str) -> tuple[str | None, int]:
    token = next((part[7:] for part in websocket.scope.get("subprotocols", []) if part.startswith("ticket.")), None)
    if not token:
        return None, 4401
    with _WS_TICKETS_LOCK:
        item = _WS_TICKETS.pop(token, None)
    if not item or item["expires"] <= time.monotonic():
        return None, 4401
    owner_id = (os.environ.get("VEYA_TAILNET_OWNER_USER_ID") or os.environ.get("VEYA_OWNER_USER_ID") or "").strip()
    if owner_id and item["user_id"] != owner_id:
        return None, 4403
    if item["profile_id"] != profile_id:
        return None, 4403
    return item["user_id"], 0


async def _dispatch_cdp_input(cdp: Any, message: dict[str, Any]) -> None:
    kind = message.get("type")
    if kind == "mouse":
        event_type = message.get("eventType")
        x, y = float(message.get("x", 0)), float(message.get("y", 0))
        if event_type not in {"mouseMoved", "mousePressed", "mouseReleased", "mouseWheel"} or not (0 <= x <= 20000 and 0 <= y <= 20000):
            raise ValueError("invalid mouse event")
        button = message.get("button", "left")
        if button not in {"none", "left", "middle", "right", "back", "forward"}:
            raise ValueError("invalid mouse button")
        params = {"type": event_type, "x": x, "y": y, "button": button, "clickCount": max(1, min(3, int(message.get("clickCount", 1))))}
        if event_type == "mouseWheel":
            params["deltaX"] = max(-10000, min(10000, float(message.get("deltaX", 0))))
            params["deltaY"] = max(-10000, min(10000, float(message.get("deltaY", 0))))
        await cdp.send("Input.dispatchMouseEvent", params)
    elif kind == "keyboard":
        event_type, key = message.get("eventType"), str(message.get("key", ""))[:64]
        if event_type not in {"keyDown", "keyUp", "rawKeyDown", "char"} or not key:
            raise ValueError("invalid keyboard event")
        await cdp.send("Input.dispatchKeyEvent", {"type": event_type, "key": key, "code": str(message.get("code", ""))[:64], "windowsVirtualKeyCode": int(message.get("windowsVirtualKeyCode", 0)) % 65536})
    elif kind == "text":
        text = str(message.get("text", ""))
        if len(text) > 8192:
            raise ValueError("text input too large")
        await cdp.send("Input.insertText", {"text": text})
    else:
        raise ValueError("unsupported CDP input")


async def _forward_cdp_frame(websocket: WebSocket, cdp: Any, frame: dict[str, Any]) -> None:
    meta = frame.get("metadata", {})
    await websocket.send_json({"type": "frame", "data": frame.get("data", ""), "metadata": {"width": meta.get("deviceWidth", 1280), "height": meta.get("deviceHeight", 800)}})
    await cdp.send("Page.screencastFrameAck", {"sessionId": frame["sessionId"]})


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
                topic="mab_tool_execution",
                payload={
                    "tool": "browser_profile_session",
                    "profile": profile,
                    "action": action,
                    "domain": domain,
                    "timestamp": time.time()
                }
            )
        except Exception as exc:
            raise RuntimeError("MAB audit event sink unavailable; refusing browser action") from exc

        # Execute actual mab_manager.perform_action via event loop bridge
        try:
            loop = mab_manager._loop
            if loop is not None and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    mab_manager.perform_action(profile, action, url=url, selector=selector, text=text),
                    loop
                )
                res = future.result(timeout=25)
            else:
                res = asyncio.run(
                    mab_manager.perform_action(profile, action, url=url, selector=selector, text=text)
                )
            return {"success": True, **res}
        except Exception as e:
            return {"success": False, "error": str(e)}

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
