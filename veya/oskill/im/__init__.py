"""veya.im — Layer 4 IM gateway adapters + account binding.

Supported platforms:
  - Feishu (Lark) — enterprise bot gateway
  - Slack — Events API bot gateway
  - Discord — Interactions Endpoint + slash commands
  - Telegram — long-polling + webhook bot
  - DingTalk (钉钉) — enterprise bot + group chat bot
  - WeChat (微信) / WeCom (企业微信) — Official Account + Enterprise WeChat

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

from veya.oskill.im.account_binding import (
    AccountBinding,
    BindingStore,
    bind_account,
    get_binding_store,
    get_user_credentials,
    inject_user_credentials,
    list_user_bindings,
    unbind_account,
)
from veya.oskill.im.dingtalk import (
    format_dingtalk_message,
)
from veya.oskill.im.dingtalk import (
    format_error as format_dingtalk_error,
)
from veya.oskill.im.dingtalk import (
    format_help as format_dingtalk_help,
)
from veya.oskill.im.dingtalk import (
    format_status as format_dingtalk_status,
)
from veya.oskill.im.dingtalk import (
    format_task_result as format_dingtalk_task_result,
)
from veya.oskill.im.dingtalk import (
    format_task_started as format_dingtalk_task_started,
)
from veya.oskill.im.feishu import (
    format_error as format_feishu_error,
)
from veya.oskill.im.feishu import (
    format_feishu_message,
)
from veya.oskill.im.feishu import (
    format_help as format_feishu_help,
)
from veya.oskill.im.feishu import (
    format_status as format_feishu_status,
)
from veya.oskill.im.feishu import (
    format_task_result as format_feishu_task_result,
)
from veya.oskill.im.feishu import (
    format_task_started as format_feishu_task_started,
)
from veya.oskill.im.pseudo import PseudoAnonymizer, anonymize_user_id, resolve_anonymizer
from veya.oskill.im.slack import (
    format_error as format_slack_error,
)
from veya.oskill.im.slack import (
    format_help as format_slack_help,
)
from veya.oskill.im.slack import (
    format_slack_message,
)
from veya.oskill.im.slack import (
    format_status as format_slack_status,
)
from veya.oskill.im.slack import (
    format_task_result as format_slack_task_result,
)
from veya.oskill.im.slack import (
    format_task_started as format_slack_task_started,
)
from veya.oskill.im.telegram import (
    format_error as format_telegram_error,
)
from veya.oskill.im.telegram import (
    format_help as format_telegram_help,
)
from veya.oskill.im.telegram import (
    format_status as format_telegram_status,
)
from veya.oskill.im.telegram import (
    format_task_result as format_telegram_task_result,
)
from veya.oskill.im.telegram import (
    format_task_started as format_telegram_task_started,
)

# Pure per-platform formatters (oskill). Platform HTTP/crypto lives in
# veya/obase/channels; the transport adapters live in server/channels.
from veya.oskill.im.telegram import (
    format_telegram_message,
)
from veya.oskill.im.wecom import (
    format_error as format_wecom_error,
)
from veya.oskill.im.wecom import (
    format_help as format_wecom_help,
)
from veya.oskill.im.wecom import (
    format_status as format_wecom_status,
)
from veya.oskill.im.wecom import (
    format_task_result as format_wecom_task_result,
)
from veya.oskill.im.wecom import (
    format_task_started as format_wecom_task_started,
)
from veya.oskill.im.wecom import (
    format_wecom_message,
)

__all__ = [  # noqa: RUF022 - grouped by platform, not alphabetical
    "AccountBinding",
    "BindingStore",
    "PseudoAnonymizer",
    "__version__",
    "anonymize_user_id",
    "bind_account",
    "get_binding_store",
    "get_user_credentials",
    "inject_user_credentials",
    "list_user_bindings",
    "resolve_anonymizer",
    "unbind_account",
    # Telegram
    "format_telegram_message",
    "format_telegram_task_started",
    "format_telegram_task_result",
    "format_telegram_error",
    "format_telegram_status",
    "format_telegram_help",
    # Feishu
    "format_feishu_message",
    "format_feishu_task_started",
    "format_feishu_task_result",
    "format_feishu_error",
    "format_feishu_status",
    "format_feishu_help",
    # DingTalk
    "format_dingtalk_message",
    "format_dingtalk_task_started",
    "format_dingtalk_task_result",
    "format_dingtalk_error",
    "format_dingtalk_status",
    "format_dingtalk_help",
    # WeCom
    "format_wecom_message",
    "format_wecom_task_started",
    "format_wecom_task_result",
    "format_wecom_error",
    "format_wecom_status",
    "format_wecom_help",
    # Slack
    "format_slack_message",
    "format_slack_task_started",
    "format_slack_task_result",
    "format_slack_error",
    "format_slack_status",
    "format_slack_help",
]
