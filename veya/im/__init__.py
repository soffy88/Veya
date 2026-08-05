"""veya.im — Layer 4 IM gateway adapters + account binding.

Supported platforms:
  - Feishu (Lark) — enterprise bot gateway
  - Slack — Events API bot gateway
  - Discord — Interactions Endpoint + slash commands
  - Telegram — long-polling + webhook bot
  - DingTalk (钉钉) — enterprise bot + group chat bot
  - WeChat (微信) — Official Account + Enterprise WeChat

Pseudo-anonymization follows SPEC §5.5.1: a stable, non-PII reference token is
derived from a raw ``user_id`` so logs / trails / memory never store personal
data.  The resolver first asks the 3O ``obase`` library for its
``pseudo_anonymizer`` element, and otherwise uses the Layer-4 HMAC-SHA256
implementation in this package.

Account binding (§5.7): users can bind their own API keys and platform tokens,
enabling per-user isolation for LLM providers and IM accounts.
"""

from __future__ import annotations

__version__ = "0.2.0"

from veya.im.pseudo import PseudoAnonymizer, anonymize_user_id, resolve_anonymizer
from veya.im.account_binding import (
    AccountBinding,
    BindingStore,
    bind_account,
    get_binding_store,
    get_user_credentials,
    inject_user_credentials,
    list_user_bindings,
    unbind_account,
)

__all__ = [
    "PseudoAnonymizer",
    "__version__",
    "AccountBinding",
    "BindingStore",
    "anonymize_user_id",
    "bind_account",
    "get_binding_store",
    "get_user_credentials",
    "inject_user_credentials",
    "list_user_bindings",
    "resolve_anonymizer",
    "unbind_account",
]
