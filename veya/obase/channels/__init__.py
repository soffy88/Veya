"""veya/obase/channels — 3O obase layer: HTTP/crypto clients for chat platforms.

Single-source rule (§1.4): this package is the canonical home for all external
chat-platform IO (Telegram, Feishu, DingTalk, WeCom). Pure formatting logic
lives in veya/oskill/im/ with re-exports here.

Dependency direction (MUST): obase → NO 3O layers (oprim/oskill/omodul/oservi).
This package may only import: stdlib, third-party, veya.errors, veya.compat,
and internal veya.obase modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Platform identifiers
PLATFORM_TELEGRAM = "telegram"
PLATFORM_FEISHU = "feishu"
PLATFORM_DINGTALK = "dingtalk"
PLATFORM_WECOM = "wecom"

PLATFORMS = (PLATFORM_TELEGRAM, PLATFORM_FEISHU, PLATFORM_DINGTALK, PLATFORM_WECOM)


@dataclass(frozen=True)
class ChannelMessage:
    """Normalized inbound message from any chat platform."""
    platform: str
    external_chat_id: str
    external_user_id: str
    text: str
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class ChannelReply:
    """Normalized outbound message to any chat platform."""
    platform: str
    external_chat_id: str
    text: str
    parse_mode: str | None = None
    extra: dict[str, Any] | None = None


# ---- Signature verification utilities (oprim: single crypto op) ----

def verify_telegram_signature(
    *,
    bot_token: str,
    payload: dict[str, Any],
    secret_token: str | None = None,
) -> bool:
    """Verify Telegram webhook secret_token (if configured)."""
    if secret_token is None:
        return True
    # Telegram sends secret_token in header X-Telegram-Bot-Api-Secret-Token
    # but we also accept it in payload for polling compatibility
    return payload.get("secret_token") == secret_token


def verify_feishu_signature(
    *,
    app_id: str,
    app_secret: str,
    payload: dict[str, Any],
    timestamp: str,
    nonce: str,
    signature: str,
) -> bool:
    """Verify Feishu/Lark event subscription signature."""
    import hashlib
    import hmac

    # Feishu: sha256(timestamp + nonce + encrypt_key)
    # For event subscription, they sign: timestamp + "\n" + nonce + "\n" + encrypt_key
    # where encrypt_key is derived from app_secret
    # See: https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/security-guide
    string_to_sign = f"{timestamp}\n{nonce}\n{app_secret}"
    expected = hmac.new(
        app_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_dingtalk_signature(
    *,
    webhook_secret: str,
    timestamp: str,
    sign: str,
) -> bool:
    """Verify DingTalk group chat bot webhook signature.

    DingTalk: base64(HMAC-SHA256(timestamp + "\n" + secret))
    """
    import base64
    import hashlib
    import hmac

    string_to_sign = f"{timestamp}\n{webhook_secret}"
    expected = hmac.new(
        webhook_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_b64 = base64.b64encode(expected).decode("utf-8")
    return hmac.compare_digest(expected_b64, sign)


def verify_wecom_signature(
    *,
    token: str,
    encoding_aes_key: str,
    payload: dict[str, Any],
    msg_signature: str,
    timestamp: str,
    nonce: str,
) -> bool:
    """Verify WeCom callback signature.

    WeCom: SHA1(sort(token, timestamp, nonce, encrypt))
    """
    import hashlib

    # Extract encrypt from payload
    encrypt = payload.get("Encrypt", "")
    tmp_list = sorted([token, timestamp, nonce, encrypt])
    tmp_str = "".join(tmp_list)
    expected = hashlib.sha1(tmp_str.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, msg_signature)


# ---- Chunking helpers (oprim: pure string manipulation) ----

TELEGRAM_MAX_LEN = 4096
FEISHU_MAX_LEN = 1900  # conservative for post cards
DINGTALK_MAX_LEN = 5000
WECOM_MAX_LEN = 2048

PLATFORM_MAX_LEN = {
    PLATFORM_TELEGRAM: TELEGRAM_MAX_LEN,
    PLATFORM_FEISHU: FEISHU_MAX_LEN,
    PLATFORM_DINGTALK: DINGTALK_MAX_LEN,
    PLATFORM_WECOM: WECOM_MAX_LEN,
}


def chunk_text(text: str, *, platform: str) -> list[str]:
    """Split text into platform-sized chunks."""
    max_len = PLATFORM_MAX_LEN.get(platform, 4096)
    if len(text) <= max_len:
        return [text]
    chunks = []
    for i in range(0, len(text), max_len):
        chunks.append(text[i : i + max_len])
    return chunks


# ---- Binding table schema (Layer 4 stores this, but types live here) ----

@dataclass(frozen=True)
class ChannelBinding:
    """Binding from external chat to Veya bot/group + owner."""
    platform: str
    external_chat_id: str
    external_user_id: str  # the user who created the binding
    vea_target_type: str  # "bot" or "group"
    vea_target_id: str    # bot_id or group_id
    owner_user_id: str    # Veya user id (pseudo-anonymized)
    created_at: float
    binding_code: str | None = None  # one-time code for linking flow


# Re-export oskill formatters (single-source guard)
# These will be populated after oskill.im migration
__all__ = [
    "PLATFORM_TELEGRAM",
    "PLATFORM_FEISHU",
    "PLATFORM_DINGTALK",
    "PLATFORM_WECOM",
    "PLATFORMS",
    "ChannelMessage",
    "ChannelReply",
    "verify_telegram_signature",
    "verify_feishu_signature",
    "verify_dingtalk_signature",
    "verify_wecom_signature",
    "chunk_text",
    "ChannelBinding",
    "TELEGRAM_MAX_LEN",
    "FEISHU_MAX_LEN",
    "DINGTALK_MAX_LEN",
    "WECOM_MAX_LEN",
]