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
from urllib.parse import urlsplit
from fastapi import APIRouter, FastAPI, HTTPException, Request, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from veya.mab import mab_manager, MABProfile
from server.events import append_canonical_event

from server.auth import require_owner

router = APIRouter(prefix="/api/v1/mab", tags=["mab"])
_WS_TICKETS: dict[str, dict[str, Any]] = {}
_WS_TICKETS_LOCK = threading.Lock()
_WS_TICKET_TTL = 60


def is_mab_enabled() -> bool:
    return os.environ.get("VEYA_MAB_BROWSER", "OFF").strip().upper() in {"1", "TRUE", "ON", "YES"}


def verify_mab_flag() -> None:
    if not is_mab_enabled():
        raise HTTPException(status_code=404, detail="MAB browser is disabled (VEYA_MAB_BROWSER is OFF)")


class ProfileCreateRequest(BaseModel):
    name: str
    site: str
    account_label: str = "default"


class ProfileUpdateRequest(BaseModel):
    name: str | None = None
    site: str | None = None
    account_label: str | None = None
    status: str | None = None


@router.get("/profiles", dependencies=[Depends(verify_mab_flag), Depends(require_owner)])
def list_profiles(request: Request):
    profiles = mab_manager.list_profiles()
    return {"ok": True, "profiles": [vars(p) for p in profiles]}

@router.post("/profiles", dependencies=[Depends(verify_mab_flag), Depends(require_owner)])
def create_profile(payload: ProfileCreateRequest, request: Request):
    p = mab_manager.create_profile(payload.name, payload.site, payload.account_label)
    return {"ok": True, "profile": vars(p)}

@router.get("/profiles/{profile_id}", dependencies=[Depends(verify_mab_flag), Depends(require_owner)])
def get_profile(profile_id: str, request: Request):
    p = mab_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"ok": True, "profile": vars(p)}

@router.put("/profiles/{profile_id}", dependencies=[Depends(verify_mab_flag), Depends(require_owner)])
def update_profile(profile_id: str, payload: ProfileUpdateRequest, request: Request):
    p = mab_manager.update_profile(profile_id, name=payload.name, site=payload.site, account_label=payload.account_label, status=payload.status)
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"ok": True, "profile": vars(p)}

@router.delete("/profiles/{profile_id}", dependencies=[Depends(verify_mab_flag), Depends(require_owner)])
def delete_profile(profile_id: str, request: Request, confirm: bool = False):
    try:
        success = mab_manager.delete_profile(profile_id, confirm=confirm)
        if not success:
            raise HTTPException(status_code=404, detail="Profile not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "deleted": profile_id}

@router.post("/profiles/{profile_id}/revoke", dependencies=[Depends(verify_mab_flag), Depends(require_owner)])
def revoke_profile(profile_id: str, request: Request):
    """一键撤销授权（登出/禁用）"""
    p = mab_manager.update_profile(profile_id, status="disabled")
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"ok": True, "profile": vars(p)}

@router.post("/profiles/{profile_id}/enable", dependencies=[Depends(verify_mab_flag), Depends(require_owner)])
def enable_profile(profile_id: str, request: Request):
    p = mab_manager.update_profile(profile_id, status="active")
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"ok": True, "profile": vars(p)}


@router.post("/profiles/{profile_id}/stream-ticket", dependencies=[Depends(verify_mab_flag)])
def create_stream_ticket(profile_id: str, request: Request, current_user: dict[str, Any] = Depends(require_owner)):
    p = mab_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile not found")
    if p.status != "active":
        raise HTTPException(status_code=403, detail="Profile not active")
    token = secrets.token_urlsafe(32)
    user_id = str(current_user.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="Authenticated user identity missing")
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
    ticket_protocol = next(
        (part for part in websocket.scope.get("subprotocols", []) if part.startswith("ticket.")),
        None,
    )
    if ticket_protocol is None:
        await websocket.close(code=4401, reason="Ticket subprotocol required")
        return
    await websocket.accept(subprotocol=ticket_protocol)

    session = None
    cdp = None
    frame_tasks: set[asyncio.Task] = set()
    try:
        session = await mab_manager.ensure_browser(profile_id)
        page = session.get("page")
        context = session.get("context")
        if page and context:
            cdp = await context.new_cdp_session(page)
            await cdp.send("Page.enable")

            async def forward_frame(frame):
                await _forward_cdp_frame(websocket, cdp, frame)

            def on_screencast_frame(frame):
                task = asyncio.create_task(forward_frame(frame))
                frame_tasks.add(task)
                def consume_result(done):
                    frame_tasks.discard(done)
                    if not done.cancelled():
                        try:
                            done.exception()
                        except Exception:
                            pass
                task.add_done_callback(consume_result)

            cdp.on("Page.screencastFrame", on_screencast_frame)
            await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 70, "maxWidth": 1280, "maxHeight": 800, "everyNthFrame": 1})

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
    except Exception as exc:
        try:
            await websocket.close(code=1011, reason=f"Stream transport failed ({type(exc).__name__})")
        except Exception:
            pass
    finally:
        for task in tuple(frame_tasks):
            task.cancel()
        if frame_tasks:
            await asyncio.gather(*tuple(frame_tasks), return_exceptions=True)
        if cdp:
            try:
                await cdp.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                await cdp.detach()
            except Exception:
                pass



def _take_ws_ticket(websocket: Any, profile_id: str) -> tuple[str | None, int]:
    protocols = websocket.scope.get("subprotocols", [])
    token = next((part[len("ticket."):] for part in protocols if part.startswith("ticket.") and part[len("ticket."):]), None)
    if not token:
        return None, 4401
    with _WS_TICKETS_LOCK:
        item = _WS_TICKETS.pop(token, None)
    if not item or item["expires"] <= time.monotonic():
        return None, 4401
    if item.get("profile_id") != profile_id:
        return None, 4403
    if not item.get("user_id"):
        return None, 4401
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
    try:
        await websocket.send_json({
            "type": "frame",
            "data": frame.get("data", ""),
            "metadata": {
                "width": max(1, min(20000, int(meta.get("deviceWidth") or 1280))),
                "height": max(1, min(20000, int(meta.get("deviceHeight") or 800))),
            },
        })
    finally:
        # Every received CDP frame is acknowledged, including send failures when the
        # client disconnected between frame receipt and WebSocket forwarding.
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

        # Audit only the hostname: never persist URL userinfo, query strings, or fragments.
        domain = ""
        try:
            domain = urlsplit(url or p.site).hostname or ""
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
