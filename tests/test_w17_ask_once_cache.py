"""W17 clean-room: session-scoped ask-once approval cache tests."""

from __future__ import annotations

import time

import pytest

from server.ask_once_cache import (
    fingerprint_arguments,
    get_ask_once_cache,
    is_ask_once_enabled,
    is_never_cached,
)
from server.privileged_action_gate import (
    STATUS_APPROVAL_REQUIRED,
    STATUS_APPROVED,
    record_privileged_action_approval,
    request_privileged_action,
)


@pytest.fixture(autouse=True)
def clean_cache():
    cache = get_ask_once_cache()
    cache.clear_all()
    yield
    cache.clear_all()


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("VEYA_APPROVAL_ASK_ONCE", raising=False)
    assert not is_ask_once_enabled()

    cache = get_ask_once_cache()
    recorded = cache.record("s1", "some_tool", {"a": 1})
    assert not recorded
    assert cache.check("s1", "some_tool", {"a": 1}) is None


def test_ask_once_caching_and_fingerprint(monkeypatch):
    monkeypatch.setenv("VEYA_APPROVAL_ASK_ONCE", "1")
    assert is_ask_once_enabled()

    cache = get_ask_once_cache()
    args1 = {"path": "foo.txt", "mode": "w"}
    args2 = {"mode": "w", "path": "foo.txt"}  # same content, different key order
    assert fingerprint_arguments(args1) == fingerprint_arguments(args2)

    ok = cache.record("sess_1", "tool_a", args1, decision="approved", approval_id="app_123")
    assert ok

    # Hit cache
    hit = cache.check("sess_1", "tool_a", args2)
    assert hit is not None
    assert hit["decision"] == "approved"
    assert hit["approval_id"] == "app_123"

    # Miss on different session
    assert cache.check("sess_2", "tool_a", args1) is None

    # Miss on different tool
    assert cache.check("sess_1", "tool_b", args1) is None

    # Miss on different args
    assert cache.check("sess_1", "tool_a", {"path": "bar.txt"}) is None


def test_destructive_and_service_control_never_cached(monkeypatch):
    monkeypatch.setenv("VEYA_APPROVAL_ASK_ONCE", "1")
    cache = get_ask_once_cache()

    for tool in (
        "rm",
        "file_delete",
        "destroy_db",
        "kill_process",
        "systemctl_restart",
        "service_control",
    ):
        assert is_never_cached(tool)
        assert not cache.record("s1", tool, {"target": "proc1"})
        assert cache.check("s1", tool, {"target": "proc1"}) is None

    # By effect
    assert is_never_cached("custom_action", effect="destructive")
    assert is_never_cached("custom_action", effect="service_control")
    assert not cache.record("s1", "custom_action", {}, effect="destructive")


def test_protected_config_never_cached(monkeypatch):
    monkeypatch.setenv("VEYA_APPROVAL_ASK_ONCE", "1")
    cache = get_ask_once_cache()

    args_protected = {"filepath": "~/.veya/hosts.json"}
    assert is_never_cached("write_file", arguments=args_protected)
    assert not cache.record("s1", "write_file", args_protected)
    assert cache.check("s1", "write_file", args_protected) is None


def test_ttl_and_clear_session(monkeypatch):
    monkeypatch.setenv("VEYA_APPROVAL_ASK_ONCE", "1")
    monkeypatch.setenv("VEYA_SESSION_TTL_SECONDS", "0.05")
    cache = get_ask_once_cache()

    cache.record("s1", "tool1", {"k": "v"})
    assert cache.check("s1", "tool1", {"k": "v"}) is not None

    time.sleep(0.08)
    assert cache.check("s1", "tool1", {"k": "v"}) is None

    # clear_session
    cache.record("s1", "tool1", {"k": "v"})
    cache.record("s2", "tool1", {"k": "v"})
    cache.clear_session("s1")
    assert cache.check("s1", "tool1", {"k": "v"}) is None
    assert cache.check("s2", "tool1", {"k": "v"}) is not None


def test_privileged_action_gate_ask_once_integration(monkeypatch):
    monkeypatch.setenv("VEYA_APPROVAL_ASK_ONCE", "1")

    # Action that requires approval
    action = "deploy_update"
    args = {"service": "my-worker"}

    # Mock gateway to require approval
    class MockDecision:
        verdict = "APPROVAL_REQUIRED"
        reason = "needs review"

    class MockGateway:
        def __init__(self, **_):
            pass

        def evaluate_policy(self, req):
            return MockDecision()

    monkeypatch.setattr("server.privileged_action_gate.ActionGatewayAdapter", MockGateway)

    outcome1 = request_privileged_action(
        action=action, arguments=args, session_id="sess_xyz", effect="low_risk"
    )
    assert outcome1.status == STATUS_APPROVAL_REQUIRED

    # Record human approval
    record_privileged_action_approval(
        action=action,
        arguments=args,
        session_id="sess_xyz",
        approval_id="app_approved_1",
        effect="low_risk",
    )

    # Next call with same session + args is approved via cache
    outcome2 = request_privileged_action(
        action=action, arguments=args, session_id="sess_xyz", effect="low_risk"
    )
    assert outcome2.status == STATUS_APPROVED
    assert "ask-once" in outcome2.reason
