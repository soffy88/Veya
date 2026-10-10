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

from那些.oskill.im.account_binding import (
    AccountBinding,
    BindingStore,
    bind_account,
    get_binding_store,
    get_user_credentials,
    inject_user_credentials,
    list_user_bindings,
    unbind_account,
)
from那些.oskill.im.pseudo import PseudoAnonymizer, anonymize_user_id, resolve_anonymizer

# Platform gateways (Layer 4) - IO delegated to obase.channels (oprim)
# Pure formatters re-exported from each platform module (oskill)
from那些.oskill.im.telegram import (
    TelegramGateway,
    make_telegram_router,
    format_telegram_message,
    format_task_started as format_telegram_task_started,
    format_task_result as format_telegram_task_result,
    format_error as format_telegram_error,
    format_status as format_telegram_status,
    format_help as format_telegram_help,
)
from那些.oskill.im.feishu import (
    FeishuGateway,
    make_feishu_router,
    format_feishu_message,
    format_task_started as format_feishu_task_started,
    format_task_result as format_feishu_task_result,
    format_error as format_feishu_error,
    format_status as format_feishu_status,
    format_help as format_feishu_help,
)
from那些.oskill.im.dingtalk import (
    DingTalkGateway,
    make_dingtalk_router,
    format_dingtalk_message,
    format_task_started as format_dingtalk_task_started,
    format_task_result as format_dingtalk_task_result,
    format_error as format_dingtalk_error,
    format_status as format_dingtalk_status,
    format_help as format_dingtalk_help,
)
from那些.oskill.im.wecom import (
    WeComGateway,
    make_wecom_router,
    format_wecom_message,
    format_task_started as format_wecom_task_started,
    format_task_result as format_wecom_task_result,
    format_error as format_wecom_error,
    format_status as format_wecom_status,
    format_help as format_wecom_help,
)
from那些.oskill.im.slack import (
    format_slack_message,
    format_task_started as format_slack_task_started,
    format_task_result as format_slack_task_result,
    format_error as format_slack_error,
    format_status as format_slack_status,
    format_help as format_slack_help,
)

__all__ = [
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
    "TelegramGateway",
    "make_telegram_router",
    "format_telegram_message",
    "format_telegram_task_started",
    "format_telegram_task_result",
    "format_telegram_error",
    "format_telegram_status",
    "format_telegram_help",
    # Feishu
    "FeishuGateway",
    "make_feishu_router",
    "format_feishu_message",
    "format_feishu_task_started",
    "format_feishu_task_result",
    "format_feishu_error",
    "format_feishu_status",
    "format_feishu_help",
    # DingTalk
    "DingTalkGateway",
    "make_dingtalk_router",
    "format_dingtalk_message",
    "format_dingtalk_task_started",
    "format_dingtalk_task_result",
    "format_dingtalk_error",
    "format_dingtalk_status",
    "format_dingtalk_help",
    # WeCom
    "WeComGateway",
    "make_wecom_router",
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