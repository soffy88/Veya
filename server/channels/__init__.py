"""server/channels — NANO-W7 chat-app channel adapter layer (Layer 4).

Framework-free transport adapters. Each adapter verifies the inbound webhook
signature, normalises the payload into a ``ChannelMessage``, and posts
(chunked) replies. HTTP routing lives in ``server/routes/channels.py``.

Layering:
    server/channels  -> veya.obase.channels  (HTTP + crypto clients)
                     -> veya.oskill.im       (pure formatters)
    server/routes    -> server/channels      (FastAPI wiring only)
"""

from __future__ import annotations

from .base import ChannelAdapter, SignatureRejected
from .bindings import ChannelBindingStore, binding_store, reset_binding_store
from .clients import ChannelNotConfigured, client_for, reset_clients
from .dingtalk import DingTalkAdapter
from .dispatch import DispatchOutcome, anonymous_ref, dispatch_inbound
from .feishu import FeishuAdapter
from .linking import CodeStore, LinkRequest, claim_code, codes, issue_code
from .telegram import TelegramAdapter
from .wecom import WeComAdapter

_ADAPTERS: dict[str, type[ChannelAdapter]] = {
    TelegramAdapter.platform: TelegramAdapter,
    FeishuAdapter.platform: FeishuAdapter,
    DingTalkAdapter.platform: DingTalkAdapter,
    WeComAdapter.platform: WeComAdapter,
}


def adapter_for(platform: str) -> ChannelAdapter:
    """Build the adapter for ``platform``."""
    try:
        factory = _ADAPTERS[platform]
    except KeyError:
        raise KeyError(f"unsupported channel platform: {platform}") from None
    return factory()


__all__ = [
    "ChannelAdapter",
    "ChannelBindingStore",
    "ChannelNotConfigured",
    "CodeStore",
    "DingTalkAdapter",
    "DispatchOutcome",
    "FeishuAdapter",
    "LinkRequest",
    "SignatureRejected",
    "TelegramAdapter",
    "WeComAdapter",
    "adapter_for",
    "anonymous_ref",
    "binding_store",
    "claim_code",
    "client_for",
    "codes",
    "dispatch_inbound",
    "issue_code",
    "reset_binding_store",
    "reset_clients",
]
