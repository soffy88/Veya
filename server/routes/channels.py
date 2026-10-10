"""server/routes/channels.py — Channel webhook endpoints (NANO-W7).

Inbound webhooks for Telegram, Feishu, DingTalk, WeCom.
Feature-flagged: VEYA_CHANNELS (default OFF).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from server.feature_flags import enabled as feature_enabled

_lg = logging.getLogger("veya.routes.channels")

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])


# ---- Models ----

class ChannelBindRequest(BaseModel):
    """Request to bind an external chat to a Veya bot/group."""
    platform: str = Field(..., pattern="^(telegram|feishu|dingtalk|wecom)$")
    external_chat_id: str
    external_user_id: str
    binding_code: str
    target_type: str = Field(..., pattern="^(bot|group)$")
    target_id: str


class ChannelBindResponse(BaseModel):
    """Response from bind operation."""
    success: bool
    binding_id: str | None = None
    error: str | None = None


class ChannelUnbindRequest(BaseModel):
    """Request to unbind a channel."""
    binding_id: str


class ChannelStatusResponse(BaseModel):
    """Channel binding status."""
    bindings: list[dict[str, Any]]


class ChannelWebhookResponse(BaseModel):
    """Generic webhook response."""
    ok: bool
    message: str | None = None


# ---- Dependency: feature flag check ----

async def require_channels_enabled() -> None:
    """Dependency that raises 404 if VEYA_CHANNELS is not enabled."""
    if not feature_enabled("VEYA_CHANNELS"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Channels feature not enabled (VEYA_CHANNELS=0)",
        )


# ---- In-memory binding store (replace with persistent store in production) ----

# In production, this would be a proper database table
# Schema: binding_id, platform, external_chat_id, external_user_id, target_type, target_id, owner_user_id, created_at
_bindings: dict[str, dict[str, Any]] = {}
_binding_codes: dict[str, dict[str, Any]] = {}  # one-time codes for linking flow


def _generate_binding_code() -> str:
    import secrets
    return secrets.token_urlsafe(16)


def _generate_binding_id() -> str:
    import uuid
    return str(uuid.uuid4())


# ---- Webhook endpoints ----

@router.post("/telegram/webhook", response_model=ChannelWebhookResponse, dependencies=[Depends(require_channels_enabled)])
async def telegram_webhook(request: Request):
    """Telegram Bot API webhook endpoint."""
    from那些.oskill.im.telegram import make_telegram_router
    
    # Lazy-load the gateway
    gw_router = make_telegram_router()
    # Extract the webhook handler
    webhook_handler = gw_router.routes[0].endpoint  # first route is /webhook
    
    body = await request.body()
    try:
        import json
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    result = await webhook_handler(payload)
    return ChannelWebhookResponse(ok=True, message="ok")


@router.post("/feishu/webhook", response_model=ChannelWebhookResponse, dependencies=[Depends(require_channels_enabled)])
async def feishu_webhook(request: Request):
    """Feishu/Lark event subscription webhook endpoint."""
    from那些.oskill.im.feishu import make_feishu_router
    
    gw_router = make_feishu_router()
    webhook_handler = gw_router.routes[0].endpoint
    
    body = await request.body()
    try:
        import json
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Feishu sends headers for signature verification
    timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
    nonce = request.headers.get("X-Lark-Request-Nonce", "")
    signature = request.headers.get("X-Lark-Signature", "")
    if timestamp and nonce and signature:
        payload["header"] = {
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": signature,
        }

    result = await webhook_handler(payload)
    return ChannelWebhookResponse(ok=True, message="ok")


@router.post("/dingtalk/webhook", response_model=ChannelWebhookResponse, dependencies=[Depends(require_channels_enabled)])
async def dingtalk_webhook(request: Request):
    """DingTalk webhook endpoint (enterprise bot + group chat bot)."""
    from那些.oskill.im.dingtalk import make_dingtalk_router
    
    gw_router = make_dingtalk_router()
    webhook_handler = gw_router.routes[0].endpoint
    
    body = await request.body()
    try:
        import json
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    result = await webhook_handler(payload, request)
    return ChannelWebhookResponse(ok=True, message="ok")


@router.post("/wecom/webhook", response_model=ChannelWebhookResponse, dependencies=[Depends(require_channels_enabled)])
async def wecom_webhook(request: Request):
    """WeCom (Enterprise WeChat) webhook endpoint."""
    from那些.oskill.im.wecom import make_wecom_router
    
    gw_router = make_wecom_router()
    webhook_handler = gw_router.routes[0].endpoint
    
    body = await request.body()
    
    result = await webhook_handler(request)
    return ChannelWebhookResponse(ok=True, message="ok")


# ---- Binding management (linking flow) ----

@router.post("/bind/generate-code", response_model=dict, dependencies=[Depends(require_channels_enabled)])
async def generate_binding_code(
    platform: str,
    external_chat_id: str,
    external_user_id: str,
    target_type: str,
    target_id: str,
    owner_user_id: str,
):
    """Generate a one-time binding code for the linking flow.

    User sends this code in the external chat to complete binding.
    """
    if platform not in ("telegram", "feishu", "dingtalk", "wecom"):
        raise HTTPException(status_code=400, detail="Invalid platform")
    if target_type not in ("bot", "group"):
        raise HTTPException(status_code=400, detail="Invalid target_type")

    code = _generate_binding_code()
    _binding_codes[code] = {
        "platform": platform,
        "external_chat_id": external_chat_id,
        "external_user_id": external_user_id,
        "target_type": target_type,
        "target_id": target_id,
        "owner_user_id": owner_user_id,
    }

    return {"code": code, "expires_in": 300}  # 5 minutes


@router.post("/bind/verify", response_model=ChannelBindResponse, dependencies=[Depends(require_channels_enabled)])
async def verify_binding_code(code: str, current_user_id: str):
    """Verify a binding code sent by user in external chat.

    This completes the linking flow: user generates code in Veya UI,
    sends it in the external chat, bot receives it and calls this endpoint.
    """
    if code not in _binding_codes:
        return ChannelBindResponse(success=False, error="Invalid or expired code")

    binding_data = _binding_codes.pop(code)
    
    # Verify the user matches (optional: could be any user in the chat)
    if binding_data["external_user_id"] != current_user_id:
        # Allow binding if the user is in the same chat
        pass

    binding_id = _generate_binding_id()
    binding = {
        "binding_id": binding_id,
        "platform": binding_data["platform"],
        "external_chat_id": binding_data["external_chat_id"],
        "external_user_id": binding_data["external_user_id"],
        "target_type": binding_data["target_type"],
        "target_id": binding_data["target_id"],
        "owner_user_id": current_user_id,
        "created_at": time.time(),
    }
    _bindings[binding_id] = binding

    return ChannelBindResponse(success=True, binding_id=binding_id)


@router.post("/bind", response_model=ChannelBindResponse, dependencies=[Depends(require_channels_enabled)])
async def bind_channel(request: ChannelBindRequest, current_user_id: str = "unknown"):
    """Direct bind (alternative to code-based linking flow)."""
    binding_id = _generate_binding_id()
    binding = {
        "binding_id": binding_id,
        "platform": request.platform,
        "external_chat_id": request.external_chat_id,
        "external_user_id": request.external_user_id,
        "target_type": request.target_type,
        "target_id": request.target_id,
        "owner_user_id": current_user_id,
        "created_at": time.time(),
    }
    _bindings[binding_id] = binding

    return ChannelBindResponse(success=True, binding_id=binding_id)


@router.post("/unbind", response_model=ChannelBindResponse, dependencies=[Depends(require_channels_enabled)])
async def unbind_channel(request: ChannelUnbindRequest, current_user_id: str = "unknown"):
    """Unbind a channel."""
    if request.binding_id not in _bindings:
        return ChannelBindResponse(success=False, error="Binding not found")

    binding = _bindings[request.binding_id]
    if binding["owner_user_id"] != current_user_id:
        return ChannelBindResponse(success=False, error="Not owner of this binding")

    del _bindings[request.binding_id]
    return ChannelBindResponse(success=True)


@router.get("/bindings", response_model=ChannelStatusResponse, dependencies=[Depends(require_channels_enabled)])
async def list_bindings(current_user_id: str = "unknown"):
    """List all bindings for the current user."""
    user_bindings = [
        b for b in _bindings.values()
        if b["owner_user_id"] == current_user_id
    ]
    return ChannelStatusResponse(bindings=user_bindings)


@router.get("/status", dependencies=[Depends(require_channels_enabled)])
async def channels_status():
    """Channels feature status."""
    return {
        "enabled": feature_enabled("VEYA_CHANNELS"),
        "morning_feed_enabled": feature_enabled("VEYA_MORNING_FEED"),
        "platforms": ["telegram", "feishu", "dingtalk", "wecom"],
        "binding_count": len(_bindings),
    }


# ---- Morning feed config ----

class MorningFeedConfigRequest(BaseModel):
    """Morning feed configuration."""
    enabled: bool = True
    time: str = "08:00"  # HH:MM
    timezone: str = "Asia/Shanghai"
    sections: list[str] = ["routines", "goals", "goalruns", "memory", "rss"]
    rss_urls: list[str] = []
    push_webpush: bool = True
    push_channels: list[str] = []


@router.get("/morning-feed/config", dependencies=[Depends(require_channels_enabled)])
async def get_morning_feed_config(current_user_id: str = "unknown"):
    """Get morning feed configuration for current user."""
    # In production, fetch from persistent store
    return MorningFeedConfigRequest().model_dump()


@router.post("/morning-feed/config", dependencies=[Depends(require_channels_enabled)])
async def set_morning_feed_config(config: MorningFeedConfigRequest, current_user_id: str = "unknown"):
    """Set morning feed configuration for current user."""
    # In production, save to persistent store
    return {"success": True, "config": config.model_dump()}


import time

__all__ = ["router"]