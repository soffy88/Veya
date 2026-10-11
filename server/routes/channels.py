"""server/routes/channels.py — NANO-W7 channel + morning-feed HTTP surface.

Thin wiring only (§8): signature verification, binding persistence and
delivery all live in ``server/channels`` / ``server/bot_team.morning_feed``.

Feature flags: ``VEYA_CHANNELS`` (default OFF) and ``VEYA_MORNING_FEED``
(default OFF). Both default to a 404 so the surface stays invisible until
explicitly enabled.

Identity: the current user always comes from the Authorization header via
``server.auth.get_current_user``. It is never taken from a query parameter.
The user id is pseudonymised before it is stored or handed to the omodul
(SPEC §5.5.1).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server import feature_flags
from server.bot_team.morning_feed import (
    MorningFeedConfig,
    get_config,
    save_config,
)
from server.channels import (
    ChannelBindingStore,
    LinkRequest,
    SignatureRejected,
    adapter_for,
    binding_store,
    claim_code,
    dispatch_inbound,
    issue_code,
)
from server.channels.bindings import reset_binding_store
from veya.obase.channels import PLATFORMS
from veya.oskill.im.pseudo import anonymize_user_id

_log = logging.getLogger("veya.routes.channels")

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])


# --------------------------------------------------------------------------
# Feature gating + identity
# --------------------------------------------------------------------------


def _require_flag(name: str) -> None:
    if not feature_flags.enabled(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{name} is not enabled",
        )


async def require_channels() -> None:
    """Dependency: 404 unless VEYA_CHANNELS is on."""
    _require_flag("VEYA_CHANNELS")


async def require_morning_feed() -> None:
    """Dependency: 404 unless VEYA_MORNING_FEED is on."""
    _require_flag("VEYA_MORNING_FEED")


def owner_ref(user: dict[str, Any]) -> str:
    """Stable non-PII reference for the authenticated user (§5.5.1)."""
    return anonymize_user_id(str(user.get("user_id") or ""))


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


class LinkCodeRequest(BaseModel):
    """Ask for a one-time linking code to paste into the external chat."""

    platform: str = Field(..., pattern="^(telegram|feishu|dingtalk|wecom)$")
    target_type: str = Field(..., pattern="^(bot|group)$")
    target_id: str = Field(..., min_length=1, max_length=128)


class UnbindRequest(BaseModel):
    """Remove one binding."""

    binding_id: str


class FeedConfigRequest(BaseModel):
    """Per-user morning-feed configuration."""

    enabled: bool = True
    time: str = Field("08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field("Asia/Shanghai", max_length=64)
    sections: list[str] = Field(
        default_factory=lambda: ["routines", "goals", "goalruns", "memory", "rss"]
    )
    rss_urls: list[str] = Field(default_factory=list, max_length=50)
    push_webpush: bool = True
    push_channels: list[str] = Field(default_factory=list, max_length=10)
    max_items_per_section: int = Field(5, ge=1, le=50)


# --------------------------------------------------------------------------
# Webhooks (public, authenticated by platform signature)
# --------------------------------------------------------------------------


async def _handle_webhook(request: Request, platform: str) -> dict[str, Any]:
    """Shared webhook path: verify -> parse -> bind -> dispatch -> dispatch_fn."""
    adapter = adapter_for(platform)
    payload = await _json_body(request)

    if platform == "feishu":
        challenge: dict[str, Any] | None = adapter.challenge_response(  # type: ignore[attr-defined]
            payload
        )
        if challenge is not None:
            return challenge

    try:
        adapter.check(
            payload=payload,
            headers=request.headers,
            query=dict(request.query_params),
        )
    except SignatureRejected as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    message = adapter.parse_inbound(payload=payload, headers=request.headers)
    if message is None:
        return {"ok": True, "status": "ignored"}

    # A pasted linking code completes the binding and is never executed.
    claimed = await _try_claim(
        message.text.strip(),
        platform=platform,
        external_chat_id=message.external_chat_id,
        external_user_id=message.external_user_id,
    )
    if claimed is not None:
        return claimed

    store: ChannelBindingStore = binding_store()
    binding = store.find_for_chat(
        platform=platform,
        external_chat_id=message.external_chat_id,
    )
    outcome = await dispatch_inbound(message, binding=binding, adapter=adapter)
    return outcome.to_dict()


async def _try_claim(
    text: str,
    *,
    platform: str,
    external_chat_id: str,
    external_user_id: str,
) -> dict[str, Any] | None:
    """Complete a pending link when ``text`` is a live linking code.

    The binding always records the chat the code actually arrived in, not the
    (usually unset) chat recorded when the code was issued.
    """
    if not text or len(text) > 64:
        return None
    payload = claim_code(text)
    if payload is None:
        return None
    if payload.get("platform") not in (None, platform):
        return None
    record = binding_store().bind(
        platform=platform,
        external_chat_id=external_chat_id,
        external_user_id=external_user_id,
        owner_ref=str(payload["owner_ref"]),
        target_type=str(payload["target_type"]),
        target_id=str(payload["target_id"]),
    )
    return {"ok": True, "status": "bound", "binding_id": record["binding_id"]}


async def _json_body(request: Request) -> dict[str, Any]:
    """Parse a JSON or XML webhook body into a mapping."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        import json

        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(raw.decode("utf-8", "replace"))
        except ET.ParseError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="unparseable webhook body",
            ) from exc
        return {"xml": raw.decode("utf-8", "replace"), **{c.tag: c.text for c in root}}
    return parsed if isinstance(parsed, dict) else {}


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request, _: None = Depends(require_channels)) -> dict[str, Any]:
    return await _handle_webhook(request, "telegram")


@router.post("/feishu/webhook")
async def feishu_webhook(request: Request, _: None = Depends(require_channels)) -> dict[str, Any]:
    return await _handle_webhook(request, "feishu")


@router.post("/dingtalk/webhook")
async def dingtalk_webhook(request: Request, _: None = Depends(require_channels)) -> dict[str, Any]:
    return await _handle_webhook(request, "dingtalk")


@router.get("/wecom/verify")
async def wecom_verify(
    signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
    _: None = Depends(require_channels),
) -> dict[str, Any]:
    """WeCom server-URL verification handshake."""
    ok = adapter_for("wecom").verify_url(  # type: ignore[attr-defined]
        signature=signature,
        timestamp=timestamp,
        nonce=nonce,
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="signature verification failed"
        )
    return {"echostr": echostr}


@router.post("/wecom/webhook")
async def wecom_webhook(request: Request, _: None = Depends(require_channels)) -> dict[str, Any]:
    return await _handle_webhook(request, "wecom")


# --------------------------------------------------------------------------
# Binding management (authenticated)
# --------------------------------------------------------------------------


def _require_owned_target(target_type: str, target_id: str, user: dict[str, Any]) -> None:
    """A chat can only be bound to a bot or group the caller owns (404 otherwise)."""
    uid = str(user.get("user_id") or "")
    if target_type == "bot":
        from server.goal_run.bot_identity import DEFAULT_BOT_ID, BotRegistry

        spec = BotRegistry().get(target_id)
        if spec is None and target_id != DEFAULT_BOT_ID:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bot not found")
        if spec is not None and spec.owner_id and spec.owner_id != uid:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bot not found")
    elif target_type == "group":
        from server.routes.bot_team import groups

        group = groups().get(target_id)
        owner = str(getattr(group, "user_id", "") or "") if group is not None else None
        if group is None or (owner and owner != uid):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group not found")


@router.post("/bind/code")
async def create_link_code(
    payload: LinkCodeRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_channels),
) -> dict[str, Any]:
    """Issue a one-time code the owner pastes into the external chat."""
    _require_owned_target(payload.target_type, payload.target_id, user)
    store: ChannelBindingStore = binding_store()
    binding = store.bind(
        platform=payload.platform,
        external_chat_id="",
        external_user_id="",
        owner_ref=owner_ref(user),
        target_type=payload.target_type,
        target_id=payload.target_id,
    )
    issued = issue_code(
        LinkRequest(
            platform=payload.platform,
            target_type=payload.target_type,
            target_id=payload.target_id,
            owner_ref=owner_ref(user),
            metadata={"binding_id": binding["binding_id"]},
        )
    )
    return {**issued, "binding_id": binding["binding_id"]}


@router.get("/bindings")
async def list_bindings(
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_channels),
) -> dict[str, Any]:
    """All bindings owned by the caller."""
    return {"bindings": binding_store().list(owner_ref=owner_ref(user))}


@router.post("/unbind")
async def unbind(
    payload: UnbindRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
    _: None = Depends(require_channels),
) -> dict[str, Any]:
    """Remove one of the caller's bindings."""
    removed = binding_store().unbind(payload.binding_id, owner_ref=owner_ref(user))
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="binding not found")
    return {"ok": True, "binding_id": payload.binding_id}


@router.get("/status")
async def channels_status(
    _: None = Depends(require_channels),
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Which channels are enabled and configured, plus web-reading channel ladder probe status."""
    from server.channels import adapter_for as _adapter_for
    from veya.obase.channel_probes import get_cached_channel_statuses, probe_all_channels

    configured: dict[str, bool] = {}
    for platform in PLATFORMS:
        try:
            adapter = _adapter_for(platform)
        except KeyError:
            continue
        configured[platform] = bool(
            getattr(adapter, "bot_token", None)
            or getattr(adapter, "app_secret", None)
            or getattr(adapter, "webhook_secret", None)
            or getattr(adapter, "token", None)
        )

    web_channels = get_cached_channel_statuses()
    if not web_channels:
        web_channels = await probe_all_channels()

    return {
        "channels_enabled": True,
        "morning_feed_enabled": feature_flags.enabled("VEYA_MORNING_FEED"),
        "channel_ladder_enabled": feature_flags.enabled("VEYA_CHANNEL_LADDER"),
        "platforms": list(PLATFORMS),
        "configured": configured,
        "binding_count": len(binding_store().list(owner_ref=owner_ref(user))),
        "web_channels": web_channels,
    }


# --------------------------------------------------------------------------
# Morning feed configuration (authenticated)
# --------------------------------------------------------------------------


@router.get("/morning-feed/config", dependencies=[Depends(require_morning_feed)])
async def get_feed_config(
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """The caller's morning-feed configuration."""
    return get_config(owner_ref(user)).to_dict()


@router.post("/morning-feed/config", dependencies=[Depends(require_morning_feed)])
async def set_feed_config(
    payload: FeedConfigRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Persist the caller's morning-feed configuration."""
    return save_config(MorningFeedConfig(owner_ref=owner_ref(user), **payload.model_dump()))


__all__ = ["reset_binding_store", "router"]
