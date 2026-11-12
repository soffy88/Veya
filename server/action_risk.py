"""Precise action risk classification for connector / tool calls.

Orkas-style idea, Veya implementation: every tool call is mapped to one of four
risk tiers so the approval layer can gate *outbound communication* and
*destructive* actions by default, while leaving ordinary read-only and
self-write operations alone.

Risk tiers
----------
R  read-only                       (no side effect)
W  write own data / draft          (reversible, self-scoped)
H  outbound communication          (email / message / post / webhook / notify
                                    third party / invite others)
D  destructive                     (delete, bulk delete, forwarding rule,
                                    revoke permission / access)

Unknown connector / MCP tools (names carrying an ``mcp__`` or ``connector``
prefix, or any of the recognised external prefixes) are treated as **H**
(outbound-capable) until an explicit allowlist entry downgrades them.  Veya's
own internal read-only tools are unaffected.

This module is pure and side-effect free; it performs no I/O and never raises.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

# Sentinel returned when classification cannot be determined (treated as H).
RISK_READ = "R"
RISK_WRITE = "W"
RISK_OUTBOUND = "H"
RISK_DESTRUCTIVE = "D"


def _flag_enabled(name: str, default: str) -> bool:
    raw = os.environ.get(name, default)
    if raw is None:
        return default == "1"
    return str(raw).strip() not in {"0", "false", "False", "no", "off", ""}


#: Master switch for the whole "ask before external send" behaviour.
#: Default ON (1). When OFF (0) action_risk never forces an approval — the
#: legacy behaviour is restored exactly.
def external_send_ask_enabled() -> bool:
    return _flag_enabled("VEYA_EXTERNAL_SEND_ASK", "1")


#: Tools explicitly known to be read-only (own or third-party). Anything not
#: listed is inferred by name markers / prefixes instead.
#: Veya-internal writes that look outbound/destructive by name but stay inside
#: Veya (agent-team chatter, delivery to the user's own channels, drafts).
_INTERNAL_WRITE_TOOLS = frozenset(
    {
        "team_send_message",
        "team_delete",
        "system_dispatch_omni_channel",
        "gmail_draft",
    }
)

_READ_TOOLS = frozenset(
    {
        "fetch_url",
        "browser_run",
        "grep",
        "list_files",
        "read_file_ast",
        "read_hashline",
        "long_read",
        "assemble_code_context",
        "code_blast_radius",
        "ast_grep_search",
        "get_market_data_schema",
        "search_genesis_ledger",
        "github_pr_fetch",
        "mcp_codebase",
        "mcp_stratum",
        "system_workspace_search",
        "system_workspace_reindex",
        "list_skills",
        "team_read_messages",
        "memory_search",
        "memory_get",
        "memory_recall_project_lessons",
        "runtime_calls_query",
        "read_skill",
        "wechat_discover",
        "morning_feed",
        "feed_tracker",
    }
)

#: Outbound communication (H). Includes Veya's own external channels plus the
#: named external connector tools discovered in the registry.
_OUTBOUND_TOOLS = frozenset(
    {
        # Veya's own external channels
        "send_email",
        "post_message",
        "send_message",
        "produce_wechat_article",
        "publish_article",
        "feishu_webhook",
        "webhook_send",
        "network_post",
        "http_post",
        # Named external connectors / integrations
        "slack_send",
        "slack_post",
        "gmail_send",
        "gmail_send_message",
        "calendar_create",
        "contact_create",
        "invite_user",
        "invite_member",
        "send_tweet",
        "send_sms",
        "send_dm",
        "telegram_send",
        "discord_send",
        "wechat_send",
        "publish_to",
        "post_to",
    }
)

#: Destructive actions (D).
_DESTRUCTIVE_TOOLS = frozenset(
    {
        "delete_file",
        "remove_file",
        "delete_email",
        "delete_contact",
        "calendar_delete",
        "contact_delete",
        "remove_member",
        "revoke_token",
        "revoke_access",
        "revoke_grant",
        "forward_email",
        "create_forwarding_rule",
        "unsubscribe",
        "drop_table",
        "bulk_delete",
        "purge",
        "cascade_delete",
    }
)

#: Write-own-data / drafting (W). "Save draft" lives here, not in outbound.
_WRITE_TOOLS = frozenset(
    {
        "write_file",
        "edit_hashline",
        "ast_grep_rewrite",
        "create_draft",
        "save_draft",
        "gmail_create_draft",
        "draft_email",
    }
)

#: Substrings whose presence marks a destructive tool name.
_DESTRUCTIVE_MARKERS = (
    "delete",
    "remove",
    "destroy",
    "drop",
    "revoke",
    "purge",
    "unsubscribe",
    "forward",
    "cascade_delete",
    "bulk_delete",
)

#: Substrings whose presence marks an outbound-communication tool name.
_OUTBOUND_MARKERS = (
    "send",
    "email",
    "message",
    "post",
    "publish",
    "webhook",
    "notify",
    "invite",
    "tweet",
    "dm",
    "slack",
    "gmail",
    "telegram",
    "discord",
    "wechat",
    "sms",
)

#: Substrings whose presence marks a self-write / draft tool name.
_WRITE_MARKERS = ("draft", "write", "edit", "create", "save")

#: Prefixes belonging to external / connector / MCP tool surfaces that are not
#: part of Veya's own internal tool set. Anything else *not* in this known set
#: that carries an external prefix is treated as outbound-capable (H).
#:
#: Veya's own MCP surface is a fixed, enumerated set (server gateways /
#: adapters such as ``mcp_codebase`` / ``mcp_hevi``); those must stay internal
#: and never be auto-classified as external communication. Real external (third
#: party) MCP tools arrive either namespaced (``mcp__server__action``) or as a
#: dynamic ``mcp_{server}`` / ``mcp_{server}_{action}`` for an *unknown* server,
#: or behind a ``connector_`` / ``integration_`` prefix.
_INTERNAL_MCP_TOOLS = frozenset(
    {
        "mcp_attachment",
        "mcp_bridge",
        "mcp_call",
        "mcp_codebase",
        "mcp_connect",
        "mcp_gateway",
        "mcp_hevi",
        "mcp_refresh",
        "mcp_stratum",
        "mcp_tool",
        "mcp_world_intel",
    }
)


def _looks_external(name: str) -> bool:
    lowered = name.lower()
    # Namespaced external MCP proxy: mcp__server__action (real third-party MCP).
    if lowered.startswith("mcp__"):
        return True
    # Dynamic external MCP server action / gateway for an *unknown* server.
    # Known internal Veya MCP tools are excluded so they stay R/W, not H.
    if lowered.startswith("mcp_") and lowered not in _INTERNAL_MCP_TOOLS:
        return True
    # External connector / integration surfaces.
    return bool(lowered.startswith(("connector_", "integration_")))


def _name_markers(name: str, markers: Iterable[str]) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in markers)


def _is_registered_internal(name: str) -> bool:
    """True when ``name`` is a tool in Veya's own master registry."""
    try:
        from server.tool_registry import master_tools

        return bool(master_tools.has(name))
    except Exception:
        return False


def classify_action(tool_name: str, args: dict | None = None) -> str:
    """Map ``tool_name`` to one of ``R`` / ``W`` / ``H`` / ``D``.

    Falls back through explicit tables → external prefix → destructive
    markers → write markers → outbound markers → read. Never raises.
    """
    if not tool_name:
        # Unnamed / unknown call: treat as outbound-capable to be safe.
        return RISK_OUTBOUND
    name = str(tool_name).strip()
    lowered = name.lower()

    if name in _INTERNAL_WRITE_TOOLS:
        return RISK_WRITE
    if name in _READ_TOOLS:
        return RISK_READ
    if name in _DESTRUCTIVE_TOOLS:
        return RISK_DESTRUCTIVE
    if name in _OUTBOUND_TOOLS:
        return RISK_OUTBOUND
    if name in _WRITE_TOOLS:
        return RISK_WRITE

    # Unknown external / connector / MCP tools are outbound-capable (H) until an
    # explicit allowlist entry downgrades them. This precedence also means a
    # connector tool that merely looks destructive is still treated as H.
    if _looks_external(lowered):
        return RISK_OUTBOUND

    # Name-marker heuristics only for names we cannot attribute to Veya's own
    # registry; Veya's internal tools keep their existing gates (HIGH_IMPACT,
    # HUMAN_ONLY) so autonomous flows are not newly blocked.
    if not _is_registered_internal(name):
        if _name_markers(lowered, _DESTRUCTIVE_MARKERS):
            return RISK_DESTRUCTIVE
        # Draft markers before outbound markers: draft_email is W, not H.
        if _name_markers(lowered, _WRITE_MARKERS):
            return RISK_WRITE
        if _name_markers(lowered, _OUTBOUND_MARKERS):
            return RISK_OUTBOUND
    return RISK_READ


__all__ = [
    "RISK_DESTRUCTIVE",
    "RISK_OUTBOUND",
    "RISK_READ",
    "RISK_WRITE",
    "classify_action",
    "external_send_ask_enabled",
]
