"""server/channels/clients.py — obase channel client factory (Layer 4).

Credentials come from the environment / secret store only and are never
persisted here. Clients are cached per platform for the process lifetime.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_log = logging.getLogger(__name__)

_clients: dict[str, Any] = {}


class ChannelNotConfigured(RuntimeError):
    """Raised when a platform has no credentials configured."""


def reset_clients() -> None:
    """Drop cached clients (tests)."""
    _clients.clear()


async def client_for(platform: str) -> Any:
    """Return the cached obase client for ``platform``, building it if needed."""
    if platform in _clients:
        return _clients[platform]

    from veya.obase.channels import (
        PLATFORM_DINGTALK,
        PLATFORM_FEISHU,
        PLATFORM_TELEGRAM,
        PLATFORM_WECOM,
    )
    from veya.obase.channels.dingtalk import create_dingtalk_client
    from veya.obase.channels.feishu import create_feishu_client
    from veya.obase.channels.telegram import create_telegram_client
    from veya.obase.channels.wecom import create_wecom_client

    client: Any

    if platform == PLATFORM_TELEGRAM:
        token = _require("TELEGRAM_BOT_TOKEN")
        client = await create_telegram_client(token)
    elif platform == PLATFORM_FEISHU:
        client = await create_feishu_client(
            _require("FEISHU_APP_ID"),
            _require("FEISHU_APP_SECRET"),
        )
    elif platform == PLATFORM_DINGTALK:
        client = await create_dingtalk_client(
            app_key=os.environ.get("DINGTALK_APP_KEY") or None,
            app_secret=os.environ.get("DINGTALK_APP_SECRET") or None,
            webhook_url=os.environ.get("DINGTALK_WEBHOOK_URL") or None,
            webhook_secret=os.environ.get("DINGTALK_WEBHOOK_SECRET") or None,
        )
    elif platform == PLATFORM_WECOM:
        client = await create_wecom_client(
            corp_id=os.environ.get("WECOM_CORP_ID") or None,
            corp_secret=os.environ.get("WECOM_CORP_SECRET") or None,
            agent_id=os.environ.get("WECOM_AGENT_ID") or None,
            webhook_url=os.environ.get("WECOM_WEBHOOK_URL") or None,
            token=os.environ.get("WECOM_TOKEN") or None,
            encoding_aes_key=os.environ.get("WECOM_ENCODING_AES_KEY") or None,
        )
    else:
        raise ChannelNotConfigured(f"unknown platform: {platform}")

    _clients[platform] = client
    return client


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ChannelNotConfigured(f"{name} is not configured")
    return value


__all__ = ["ChannelNotConfigured", "client_for", "reset_clients"]
