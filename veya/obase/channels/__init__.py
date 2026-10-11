"""veya/obase/channels — 3O obase layer: HTTP/crypto clients for chat platforms.

Single-source rule (§1.4): this package is the canonical home for all external
chat-platform IO (Telegram, Feishu, DingTalk, WeCom). Pure formatting logic
lives in veya/oskill/im/ with re-exports here.

Dependency direction (MUST): obase → NO 3O layers (oprim/oskill/omodul/oservi).
This package may only import: stdlib, third-party, veya.errors, veya.compat,
and internal veya.obase modules.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
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


# ---- Signature verification (obase: crypto oprims, single source) ----
# These are the canonical implementations; the per-platform clients delegate
# to them (single-source rule §1.4).


def verify_telegram_signature(*, secret_token: str | None, provided: str | None) -> bool:
    """Verify the Telegram ``X-Telegram-Bot-Api-Secret-Token`` header.

    When no secret is configured the webhook cannot be authenticated, so the
    caller must decide; this returns False rather than silently accepting.
    """
    if not secret_token:
        return False
    return hmac.compare_digest(secret_token, provided or "")


def verify_feishu_signature(
    *,
    encrypt_key: str,
    timestamp: str,
    nonce: str,
    signature: str,
) -> bool:
    """Verify the Lark/Feishu ``X-Lark-Signature`` event-subscription header.

    Documented scheme: ``hmac_sha256(key=encrypt_key, msg=timestamp + nonce +
    encrypt_key)`` hex-compared against the header.
    """
    if not (encrypt_key and timestamp and nonce and signature):
        return False
    string_to_sign = timestamp + nonce + encrypt_key
    expected = hmac.new(
        encrypt_key.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_dingtalk_signature(*, webhook_secret: str, timestamp: str, sign: str) -> bool:
    """Verify a DingTalk group-chat-bot webhook signature.

    Documented scheme: ``base64(hmac_sha256(key=secret, msg=timestamp + "\\n" +
    secret))``.
    """
    if not (webhook_secret and timestamp and sign):
        return False
    string_to_sign = f"{timestamp}\n{webhook_secret}"
    digest = hmac.new(
        webhook_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode("utf-8"), sign)


def verify_wecom_signature(
    *,
    token: str,
    encrypt: str,
    msg_signature: str,
    timestamp: str,
    nonce: str,
) -> bool:
    """Verify a WeCom callback ``msg_signature``.

    Documented scheme: ``sha1("".join(sorted([token, timestamp, nonce,
    encrypt])))``.
    """
    if not (token and timestamp and nonce and msg_signature):
        return False
    joined = "".join(sorted([token, timestamp, nonce, encrypt or ""]))
    return hmac.compare_digest(hashlib.sha1(joined.encode("utf-8")).hexdigest(), msg_signature)


def verify_wecom_url_signature(
    *,
    token: str,
    signature: str,
    timestamp: str,
    nonce: str,
) -> bool:
    """Verify the WeCom server-URL verification (GET) signature.

    Documented scheme: ``sha1("".join(sorted([token, timestamp, nonce])))``.
    """
    if not (token and timestamp and nonce and signature):
        return False
    joined = "".join(sorted([token, timestamp, nonce]))
    return hmac.compare_digest(hashlib.sha1(joined.encode("utf-8")).hexdigest(), signature)


# ---- Chunking helpers (oprim: pure string manipulation) ----

TELEGRAM_MAX_LEN = 4096
FEISHU_MAX_LEN = 1900  # conservative for post cards
DINGTALK_MAX_LEN = 5000
WECOM_MAX_LEN = 2048

DEFAULT_MAX_LEN = 4096

PLATFORM_MAX_LEN = {
    PLATFORM_TELEGRAM: TELEGRAM_MAX_LEN,
    PLATFORM_FEISHU: FEISHU_MAX_LEN,
    PLATFORM_DINGTALK: DINGTALK_MAX_LEN,
    PLATFORM_WECOM: WECOM_MAX_LEN,
}


def chunk_text(text: str, *, platform: str, max_len: int | None = None) -> list[str]:
    """Split ``text`` into platform-sized chunks.

    ``max_len`` overrides the platform default (callers may know a stricter
    per-deployment budget than the platform maximum).
    """
    limit = max_len if max_len is not None else PLATFORM_MAX_LEN.get(platform, DEFAULT_MAX_LEN)
    if limit <= 0:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [""]


# ---- Binding table schema (Layer 4 stores this, but types live here) ----


@dataclass(frozen=True)
class ChannelBinding:
    """Binding from external chat to Veya bot/group + owner."""

    platform: str
    external_chat_id: str
    external_user_id: str  # the user who created the binding
    vea_target_type: str  # "bot" or "group"
    vea_target_id: str  # bot_id or group_id
    owner_user_id: str  # Veya user id (pseudo-anonymized)
    created_at: float
    binding_code: str | None = None  # one-time code for linking flow


__all__ = [
    "DEFAULT_MAX_LEN",
    "DINGTALK_MAX_LEN",
    "FEISHU_MAX_LEN",
    "PLATFORMS",
    "PLATFORM_DINGTALK",
    "PLATFORM_FEISHU",
    "PLATFORM_MAX_LEN",
    "PLATFORM_TELEGRAM",
    "PLATFORM_WECOM",
    "TELEGRAM_MAX_LEN",
    "WECOM_MAX_LEN",
    "ChannelBinding",
    "ChannelMessage",
    "ChannelReply",
    "chunk_text",
    "verify_dingtalk_signature",
    "verify_feishu_signature",
    "verify_telegram_signature",
    "verify_wecom_signature",
    "verify_wecom_url_signature",
]
