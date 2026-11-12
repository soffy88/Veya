"""Action risk classification + default-ask-for-external-send gating.

Covers: the four-tier classification table, unknown external tools → H, draft → W,
H triggering approval even in autonomous mode (gates OFF, so the W6 gate is *not*
what gates it), session grants releasing H, ALWAYS grants NOT releasing D, and the
VEYA_EXTERNAL_SEND_ASK switch restoring legacy.

The integration tests deliberately run with approval gates OFF and stub
``user_control._wait_approval`` so we exercise the *new* Orkas-style branch
directly — no W6 gate, no real sqlite wait loop, no hang.
"""

from __future__ import annotations

import asyncio

import pytest

from server import approvals as approvals_mod
from server import user_control as uc
from server.action_risk import (
    RISK_DESTRUCTIVE,
    RISK_OUTBOUND,
    RISK_READ,
    RISK_WRITE,
    classify_action,
    external_send_ask_enabled,
)


@pytest.fixture(autouse=True)
def _reset_globals():
    """Isolate the module-level grant caches between tests."""
    approvals_mod.session_grants.clear()
    uc._approved_once.clear()
    yield
    approvals_mod.session_grants.clear()
    uc._approved_once.clear()


@pytest.fixture()
def user_ctx():
    from server import auth as auth_mod

    tok = auth_mod._user_ctx.set({"user_id": "user-1", "username": "user-1"})
    sid = uc._session_id.set("session-1")
    yield "user-1"
    uc._session_id.reset(sid)
    auth_mod._user_ctx.reset(tok)


@pytest.fixture()
def temp_service(tmp_path):
    """Bind the approval service to a temp store so ALWAYS grants are real."""
    store = approvals_mod.configure_service(
        store=__import__("veya.obase.approvals", fromlist=["ApprovalStore"]).ApprovalStore(
            str(tmp_path / "approvals.db")
        )
    )
    yield store


def _activate(require_approval: bool = False):
    return uc.activate(mode="agent", require_approval=require_approval, session_id="session-1")


def _deactivate(tokens):
    uc.deactivate(tokens)


# ── classification table ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tool,expected",
    [
        # read-only (own + third-party)
        ("fetch_url", RISK_READ),
        ("browser_run", RISK_READ),
        ("grep", RISK_READ),
        ("list_files", RISK_READ),
        ("read_file_ast", RISK_READ),
        ("mcp_codebase", RISK_READ),
        ("team_read_messages", RISK_READ),
        ("github_pr_fetch", RISK_READ),
        # write own data / draft
        ("write_file", RISK_WRITE),
        ("edit_hashline", RISK_WRITE),
        ("create_draft", RISK_WRITE),
        ("save_draft", RISK_WRITE),
        ("gmail_create_draft", RISK_WRITE),
        ("draft_email", RISK_WRITE),
        # outbound communication (H)
        ("send_email", RISK_OUTBOUND),
        ("post_message", RISK_OUTBOUND),
        ("send_message", RISK_OUTBOUND),
        ("team_send_message", RISK_WRITE),
        ("produce_wechat_article", RISK_OUTBOUND),
        ("feishu_webhook", RISK_OUTBOUND),
        ("webhook_send", RISK_OUTBOUND),
        ("slack_send", RISK_OUTBOUND),
        ("gmail_send", RISK_OUTBOUND),
        ("invite_user", RISK_OUTBOUND),
        ("post_to", RISK_OUTBOUND),
        ("publish_to", RISK_OUTBOUND),
        # destructive (D)
        ("delete_file", RISK_DESTRUCTIVE),
        ("remove_member", RISK_DESTRUCTIVE),
        ("revoke_token", RISK_DESTRUCTIVE),
        ("revoke_access", RISK_DESTRUCTIVE),
        ("delete_email", RISK_DESTRUCTIVE),
        ("forward_email", RISK_DESTRUCTIVE),
        ("create_forwarding_rule", RISK_DESTRUCTIVE),
        ("unsubscribe", RISK_DESTRUCTIVE),
    ],
)
def test_classification_table(tool, expected):
    assert classify_action(tool) == expected


def test_unknown_external_tool_is_outbound():
    assert classify_action("mcp__notion__write_page") == RISK_OUTBOUND
    assert classify_action("connector_salesforce_push") == RISK_OUTBOUND
    assert classify_action("mcp_linear_create_issue") == RISK_OUTBOUND
    assert classify_action("remote_calendar_invite") == RISK_OUTBOUND


def test_unknown_native_tool_defaults_read():
    # A native Veya tool with no external marker stays read by default.
    assert classify_action("some_new_internal_tool") == RISK_READ


def test_draft_is_write_not_outbound():
    assert classify_action("save_draft") == RISK_WRITE
    assert classify_action("create_draft") == RISK_WRITE


def test_destructive_marker_wins_over_outbound_marker():
    # "delete_email" contains "email" (outbound marker) but is destructive.
    assert classify_action("delete_email") == RISK_DESTRUCTIVE
    assert classify_action("revoke_access") == RISK_DESTRUCTIVE


def test_destructive_unknown_external_is_outbound():
    # Unknown external destructive-looking tool stays H (outbound-capable) unless
    # the explicit destructive table catches it. mcp__ deletions are outbound.
    assert classify_action("mcp__foo__delete_widget") == RISK_OUTBOUND


def test_external_send_ask_default_on(monkeypatch):
    monkeypatch.delenv("VEYA_EXTERNAL_SEND_ASK", raising=False)
    assert external_send_ask_enabled() is True
    monkeypatch.setenv("VEYA_EXTERNAL_SEND_ASK", "0")
    assert external_send_ask_enabled() is False
    monkeypatch.setenv("VEYA_EXTERNAL_SEND_ASK", "1")
    assert external_send_ask_enabled() is True


# ── new Orkas-style default gate (exercised with approval gates OFF) ─────
#
# With VEYA_APPROVAL_GATES disabled the W6 gate cannot be what gates an H/D
# call; the decision must come entirely from the new branch. We stub
# ``user_control._wait_approval`` so the gating decision is observable without a
# real sqlite wait loop — and without any risk of a hang.


def _stub_wait(monkeypatch):
    """Replace _wait_approval with a recorder that simply approves."""
    calls: list[str] = []

    async def _fake(tool, kwargs, on_verdict=None, sentinel=None):
        calls.append(tool)
        return None

    monkeypatch.setattr(uc, "_wait_approval", _fake)
    return calls


def test_requires_external_send_approval_tiers(user_ctx, temp_service):
    # H and D require approval by default; R and W do not.
    assert uc._requires_external_send_approval("send_email", {}, "master") is True
    assert uc._requires_external_send_approval("delete_file", {}, "master") is True
    assert uc._requires_external_send_approval("write_file", {}, "master") is False
    assert uc._requires_external_send_approval("fetch_url", {}, "master") is False


def test_internal_chat_reply_not_requires_approval(user_ctx, temp_service):
    # The bot answering in its own conversation is not an outbound send.
    assert uc._requires_external_send_approval("send_message", {}, "internal_chat") is False


def test_switch_off_restores_legacy_decision(user_ctx, temp_service, monkeypatch):
    # VEYA_EXTERNAL_SEND_ASK=0 → the new gate never fires (legacy behaviour).
    monkeypatch.setenv("VEYA_EXTERNAL_SEND_ASK", "0")
    assert uc._requires_external_send_approval("send_email", {}, "master") is False
    assert uc._requires_external_send_approval("delete_file", {}, "master") is False


def test_h_triggers_approval_in_autonomous_mode(user_ctx, temp_service, monkeypatch):
    """H call asks for approval even with require_approval=False and gates OFF."""
    calls = _stub_wait(monkeypatch)
    tokens = _activate(require_approval=False)
    try:
        assert uc._requires_external_send_approval("send_email", {}, "master") is True
        verdict = asyncio.run(
            uc.user_control_policy("send_email", {"to": "a@b.c", "body": "hi"}, "master")
        )
        # No grant, flag on, gates off → the new branch must open the wait.
        assert "send_email" in calls
        assert verdict is None
    finally:
        _deactivate(tokens)


def test_session_grant_releases_h(user_ctx, temp_service, monkeypatch):
    """A SESSION grant (not ALWAYS) lets an H call through without a pause."""
    calls = _stub_wait(monkeypatch)
    scope = approvals_mod.get_approval_service().scope_for("user-1")
    approvals_mod.session_grants.add((scope, "send_email"))
    tokens = _activate(require_approval=False)
    try:
        verdict = asyncio.run(
            uc.user_control_policy("send_email", {"to": "a@b.c", "body": "hi"}, "master")
        )
        # Session grant releases H → no wait opened; call is allowed.
        assert calls == []
        assert verdict is None
    finally:
        _deactivate(tokens)


def test_always_grant_does_not_release_d(user_ctx, temp_service, monkeypatch):
    """An ALWAYS (durable) grant must NOT release a destructive (D) call."""
    calls = _stub_wait(monkeypatch)
    scope = approvals_mod.get_approval_service().scope_for("user-1")
    # Durable ALWAYS grant for the destructive tool.
    approvals_mod.get_approval_service().store.put_grant(scope=scope, tool="delete_file")
    assert uc._external_grant_allows("delete_file", scope) is False
    tokens = _activate(require_approval=False)
    try:
        verdict = asyncio.run(uc.user_control_policy("delete_file", {"path": "/tmp/x"}, "master"))
        # D not released by ALWAYS grant → the new branch opens the wait.
        assert "delete_file" in calls
        assert verdict is None
    finally:
        _deactivate(tokens)


def test_switch_off_restores_legacy(user_ctx, temp_service, monkeypatch):
    """VEYA_EXTERNAL_SEND_ASK=0 → H call runs with no approval (legacy)."""
    calls = _stub_wait(monkeypatch)
    monkeypatch.setenv("VEYA_EXTERNAL_SEND_ASK", "0")
    tokens = _activate(require_approval=False)
    try:
        verdict = asyncio.run(
            uc.user_control_policy("send_email", {"to": "a@b.c", "body": "hi"}, "master")
        )
        assert calls == []  # new gate did not open a wait
        assert verdict is None
    finally:
        _deactivate(tokens)


def test_always_grant_releases_h(user_ctx, temp_service, monkeypatch):
    """An ALWAYS (durable) grant does release an H call (only D is blocked)."""
    calls = _stub_wait(monkeypatch)
    scope = approvals_mod.get_approval_service().scope_for("user-1")
    approvals_mod.get_approval_service().store.put_grant(scope=scope, tool="send_email")
    assert uc._external_grant_allows("send_email", scope) is True
    tokens = _activate(require_approval=False)
    try:
        verdict = asyncio.run(
            uc.user_control_policy("send_email", {"to": "a@b.c", "body": "hi"}, "master")
        )
        assert calls == []
        assert verdict is None
    finally:
        _deactivate(tokens)
