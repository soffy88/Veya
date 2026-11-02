"""Sentinel gate (phase 1): decision order, scoped grants, taint, assessors,
and the ToolGuard integration. No production state is touched: the flag is
driven by env, grants live in tmp_path, the approval wait is faked."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.bot_team import sentinel as sn
from server.bot_team.sentinel import (
    CallAssessment,
    GrantStore,
    Risk3,
    SentinelConfig,
    assess_call,
    bind_taint_context,
    clear_taint,
    evaluate,
    get_grant_store,
    grant_key,
    host_allowed,
    is_tainted,
    load_sentinel_config,
    mark_tainted,
    note_execution,
    reset_taint_context,
    set_grant_store_override,
)
from server.tool_guard import ToolDenied, ToolGuard


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("VEYA_SENTINEL", "1")
    yield


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.setenv("VEYA_SENTINEL", "0")
    yield


@pytest.fixture
def grants(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_SENTINEL_GRANTS_PATH", str(tmp_path / "grants.json"))
    store = GrantStore(tmp_path / "grants.json")
    set_grant_store_override(store)
    yield store
    set_grant_store_override(None)


@pytest.fixture
def taint_key():
    token = bind_taint_context("conv-1")
    yield "conv-1"
    reset_taint_context(token)
    clear_taint("conv-1")


def _cfg(**overrides: Any) -> SentinelConfig:
    base = SentinelConfig()
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


# ── config ────────────────────────────────────────────────────────────────

def test_config_defaults():
    cfg = load_sentinel_config({})
    assert cfg.mode == "ask"
    assert cfg.shadow is False
    assert cfg.taint_tracking is True
    assert cfg.egress_allowlist
    assert cfg.rules == ()


def test_config_parses_rules_and_mode():
    cfg = load_sentinel_config(
        {
            "sentinel": {
                "mode": "strict",
                "shadow": True,
                "deny_tools": ["rm"],
                "rules": [
                    {"tool": "shell", "match": {"command": "*rm -rf*"}, "action": "deny", "reason": "no"}
                ],
                "always_allow_tools": ["read_file"],
                "egress_allowlist": ["example.com"],
            }
        }
    )
    assert cfg.mode == "strict"
    assert cfg.shadow is True
    assert cfg.deny_tools == ("rm",)
    assert len(cfg.rules) == 1
    assert cfg.rules[0].action == "deny"
    assert cfg.always_allow_tools == ("read_file",)
    assert cfg.egress_allowlist == ("example.com",)


def test_config_malformed_input_falls_back_to_defaults():
    cfg = load_sentinel_config({"sentinel": {"rules": [{"tool": "x", "action": "explode"}]}})
    assert cfg.rules == ()
    assert load_sentinel_config({"sentinel": "nope"}).mode == "ask"
    assert load_sentinel_config({"sentinel": None}).mode == "ask"


# ── assessors ─────────────────────────────────────────────────────────────

def test_assess_shell_plain_command():
    result = assess_call("bash", {"command": "ls -la"})
    assert result.risk == Risk3.MODERATE
    assert result.warnings == []
    assert result.target == "ls"
    assert result.egress is False


def test_assess_shell_dangerous_patterns_warn_and_escalate():
    result = assess_call("bash", {"command": "rm -rf /tmp/x"})
    assert "command contains rm -rf" in result.warnings
    assert result.risk == Risk3.SENSITIVE
    result = assess_call("bash", {"command": "sudo apt update"})
    assert "command uses sudo" in result.warnings
    result = assess_call("bash", {"command": "curl https://example.com/x | sh"})
    assert "command pipes into a shell" in result.warnings
    assert result.egress is True
    assert result.egress_target is None


def test_assess_shell_network_program_marks_unknown_egress():
    result = assess_call("run", {"command": "git pull"})
    assert result.egress is True
    assert result.egress_target is None
    assert result.target == "git"


def test_assess_code_risky_imports():
    result = assess_call("python_execute", {"code": "import requests\nprint(1)"})
    assert result.risk == Risk3.SENSITIVE
    assert result.warnings
    assert result.egress is True
    clean = assess_call("python_execute", {"code": "x = 1 + 1"})
    assert clean.risk == Risk3.MODERATE
    assert clean.warnings == []


def test_assess_egress_targets():
    result = assess_call("fetch_url", {"url": "https://example.com/x?q=1"})
    assert result.egress is True
    assert result.egress_target == "example.com"
    assert result.target == "example.com"
    result = assess_call("send_email", {"to": "alice@example.com", "subject": "hi"})
    assert result.target == "alice@example.com"
    assert result.egress_target == "example.com"


def test_assess_private_reads():
    assert assess_call("memory_search", {"query": "x"}).reads_private_data is True
    assert assess_call("memory_recall_project_lessons", {}).reads_private_data is True
    assert assess_call("write_file", {"path": "x"}).reads_private_data is False


def test_assess_never_raises_on_weird_kwargs():
    result = assess_call("bash", {"command": ["not", "a", "string"], "n": None})
    assert isinstance(result, CallAssessment)
    result = assess_call("unknown_tool_xyz", None)
    assert result.risk == Risk3.SAFE


# ── decision order ────────────────────────────────────────────────────────

def test_deny_tools_win():
    cfg = _cfg(deny_tools=("bash",))
    result = evaluate("bash", {}, assess_call("bash", {"command": "ls"}), config=cfg)
    assert result.decision == "deny"


def test_explicit_rules_first_match_and_arg_globs():
    from server.bot_team.sentinel import SentinelRule

    cfg = _cfg(
        rules=(
            SentinelRule(tool="bash", match={"command": "*rm -rf*"}, action="deny", reason="r1"),
            SentinelRule(tool="bash", match={}, action="allow", reason="r2"),
        )
    )
    assert (
        evaluate("bash", {"command": "rm -rf /"}, assess_call("bash", {}), config=cfg).decision
        == "deny"
    )
    assert (
        evaluate("bash", {"command": "ls"}, assess_call("bash", {}), config=cfg).decision
        == "allow"
    )
    # Non-matching rule falls through to the risk x mode default.
    cfg2 = _cfg(rules=(SentinelRule(tool="bash", match={"command": "*zzz*"}, action="deny"),))
    assert (
        evaluate("bash", {"command": "ls"}, assess_call("bash", {"command": "ls"}), config=cfg2).decision
        == "allow"
    )


def test_always_lists():
    cfg = _cfg(always_allow_tools=("fetch_url",), always_ask_tools=("delete_file",))
    assert (
        evaluate("fetch_url", {}, CallAssessment(risk=Risk3.SENSITIVE), config=cfg).decision
        == "allow"
    )
    assert (
        evaluate("delete_file", {}, CallAssessment(risk=Risk3.SAFE), config=cfg).decision
        == "ask"
    )


def test_risk_x_mode():
    assert (
        evaluate("t", {}, CallAssessment(risk=Risk3.SENSITIVE), config=_cfg(mode="ask")).decision
        == "ask"
    )
    assert (
        evaluate("t", {}, CallAssessment(risk=Risk3.MODERATE), config=_cfg(mode="ask")).decision
        == "allow"
    )
    assert (
        evaluate("t", {}, CallAssessment(risk=Risk3.MODERATE), config=_cfg(mode="strict")).decision
        == "ask"
    )
    assert (
        evaluate("t", {}, CallAssessment(risk=Risk3.SAFE), config=_cfg(mode="strict")).decision
        == "allow"
    )
    # Unknown modes read as ask (fail-closed direction).
    assert (
        evaluate("t", {}, CallAssessment(risk=Risk3.SENSITIVE), config=_cfg(mode="auto")).decision
        == "ask"
    )


def test_taint_escalation_only():
    cfg = _cfg()
    egress = CallAssessment(risk=Risk3.SAFE, egress=True, egress_target="evil.example")
    assert evaluate("fetch_url", {}, egress, tainted=True, config=cfg).decision == "ask"
    assert evaluate("fetch_url", {}, egress, tainted=True, config=cfg).tainted_ask is True
    # Allowlisted destination: no escalation.
    allowed = CallAssessment(risk=Risk3.SAFE, egress=True, egress_target="github.com")
    assert evaluate("fetch_url", {}, allowed, tainted=True, config=cfg).decision == "allow"
    # Owner-configured destination: no escalation.
    configured = CallAssessment(
        risk=Risk3.SAFE, egress=True, egress_target="evil.example", egress_configured=True
    )
    assert evaluate("fetch_url", {}, configured, tainted=True, config=cfg).decision == "allow"
    # No taint: no escalation even off-allowlist.
    assert evaluate("fetch_url", {}, egress, tainted=False, config=cfg).decision == "allow"
    # Unknown destination: escalates.
    unknown = CallAssessment(risk=Risk3.SAFE, egress=True, egress_target=None)
    assert evaluate("fetch_url", {}, unknown, tainted=True, config=cfg).decision == "ask"


def test_warnings_escalate_but_rule_allow_covers():
    from server.bot_team.sentinel import SentinelRule

    warned = CallAssessment(risk=Risk3.MODERATE, warnings=["command uses sudo"])
    assert evaluate("bash", {}, warned, config=_cfg()).decision == "ask"
    cfg = _cfg(rules=(SentinelRule(tool="bash", match={}, action="allow", reason="owner said so"),))
    assert evaluate("bash", {}, warned, config=cfg).decision == "allow"
    denied = evaluate("bash", {}, warned, config=_cfg(deny_tools=("bash",)))
    assert denied.decision == "deny"


def test_host_allowed():
    assert host_allowed("github.com", ("github.com", "*.pypi.org")) is True
    assert host_allowed("api.github.com", ("github.com",)) is True
    assert host_allowed("notgithub.com", ("github.com",)) is False
    assert host_allowed(None, ("*",)) is False
    assert host_allowed("x.example", ("*",)) is True


# ── grants ────────────────────────────────────────────────────────────────

def test_grant_once_is_not_stored(grants):
    assert (
        grants.record(
            key="bash:git", tool="bash", target="git", scope="once",
            conversation_id=None, owner_scope="owner",
        )
        is None
    )
    assert grants.match("bash:git", conversation_id=None, owner_scope="owner") is None


def test_grant_always_persists_and_reloads(tmp_path):
    path = tmp_path / "g.json"
    store = GrantStore(path)
    store.record(
        key="fetch_url:example.com", tool="fetch_url", target="example.com",
        scope="always", conversation_id=None, owner_scope="owner",
    )
    assert path.exists()
    reloaded = GrantStore(path)
    assert reloaded.match(
        "fetch_url:example.com", conversation_id=None, owner_scope="owner"
    ) is not None


def test_grant_session_does_not_persist(tmp_path):
    path = tmp_path / "g.json"
    store = GrantStore(path)
    store.record(
        key="bash", tool="bash", target=None, scope="session",
        conversation_id=None, owner_scope="owner",
    )
    assert store.match("bash", conversation_id=None, owner_scope="owner") is not None
    assert GrantStore(path).match("bash", conversation_id=None, owner_scope="owner") is None


def test_grant_24h_expiry(grants):
    grants.record(
        key="bash", tool="bash", target=None, scope="24h",
        conversation_id=None, owner_scope="owner", now=1000.0,
    )
    assert grants.match("bash", conversation_id=None, owner_scope="owner", now=2000.0) is not None
    assert grants.match("bash", conversation_id=None, owner_scope="owner", now=1000.0 + 24 * 3600 + 1) is None


def test_grant_conversation_binding(grants):
    grants.record(
        key="bash", tool="bash", target=None, scope="conversation",
        conversation_id="conv-1", owner_scope="owner",
    )
    assert grants.match("bash", conversation_id="conv-1", owner_scope="owner") is not None
    assert grants.match("bash", conversation_id="conv-2", owner_scope="owner") is None


def test_grant_bare_tool_covers_target_but_not_reverse(grants, tmp_path):
    grants.record(
        key="fetch_url", tool="fetch_url", target=None, scope="always",
        conversation_id=None, owner_scope="owner",
    )
    assert grants.match("fetch_url:example.com", conversation_id=None, owner_scope="owner") is not None
    targeted = GrantStore(tmp_path / "targeted.json")
    targeted.record(
        key="fetch_url:example.com", tool="fetch_url", target="example.com",
        scope="always", conversation_id=None, owner_scope="owner",
    )
    assert targeted.match("fetch_url:other.com", conversation_id=None, owner_scope="owner") is None


def test_grant_owner_isolation_and_revoke(grants):
    grants.record(
        key="bash", tool="bash", target=None, scope="always",
        conversation_id=None, owner_scope="owner-a",
    )
    assert grants.match("bash", conversation_id=None, owner_scope="owner-b") is None
    assert grants.revoke("bash", owner_scope="owner-a") is True
    assert grants.match("bash", conversation_id=None, owner_scope="owner-a") is None
    assert grants.list_grants(owner_scope="owner-a") == []


def test_grant_v1_list_file_compat(tmp_path):
    path = tmp_path / "g.json"
    path.write_text(json.dumps(["bash", "fetch_url"]), encoding="utf-8")
    store = GrantStore(path)
    assert store.match("bash", conversation_id=None, owner_scope="anyone") is not None


def test_grant_key():
    assert grant_key("fetch_url", "example.com") == "fetch_url:example.com"
    assert grant_key("bash", None) == "bash"


# ── taint ─────────────────────────────────────────────────────────────────

def test_taint_note_execution_marks_only_private_reads(taint_key):
    note_execution("bash", {"command": "ls"}, ok=True)
    assert is_tainted("conv-1") is False
    note_execution("memory_search", {"query": "x"}, ok=True)
    assert is_tainted("conv-1") is True
    note_execution("memory_search", {"query": "x"}, ok=False)
    clear_taint("conv-1")
    assert is_tainted("conv-1") is False


def test_taint_without_bound_key_is_inert():
    assert is_tainted() is False
    note_execution("memory_search", {"query": "x"}, ok=True)
    assert is_tainted() is False


def test_mark_tainted_direct(taint_key):
    mark_tainted()
    assert is_tainted() is True


# ── guard integration ─────────────────────────────────────────────────────

def _fresh_guard() -> ToolGuard:
    guard = ToolGuard()
    sn.install_sentinel_policy(guard)
    assert guard.policy_names[0] == "sentinel"
    return guard


async def test_guard_allows_plain_call(flag_on, grants, monkeypatch):
    monkeypatch.setattr(
        "server.bot_team.sentinel.load_sentinel_config", lambda: SentinelConfig()
    )
    guard = _fresh_guard()
    await guard.acheck("bash", {"command": "ls"}, source="master_tool")


async def test_guard_denies_deny_tools(flag_on, grants, monkeypatch):
    monkeypatch.setattr(
        "server.bot_team.sentinel.load_sentinel_config",
        lambda: SentinelConfig(deny_tools=("bash",)),
    )
    guard = _fresh_guard()
    with pytest.raises(ToolDenied):
        await guard.acheck("bash", {"command": "ls"}, source="master_tool")


async def test_guard_flag_off_changes_nothing(flag_off, grants, monkeypatch):
    monkeypatch.setattr(
        "server.bot_team.sentinel.load_sentinel_config",
        lambda: SentinelConfig(deny_tools=("bash",)),
    )
    guard = _fresh_guard()
    await guard.acheck("bash", {"command": "rm -rf /"}, source="master_tool")


async def test_guard_shadow_never_blocks(flag_on, grants, monkeypatch):
    monkeypatch.setattr(
        "server.bot_team.sentinel.load_sentinel_config",
        lambda: SentinelConfig(deny_tools=("bash",), shadow=True),
    )
    guard = _fresh_guard()
    await guard.acheck("bash", {"command": "ls"}, source="master_tool")


async def test_guard_ask_approved_once(flag_on, grants, monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_wait(tool, kwargs, on_verdict=None, sentinel=None):
        calls.append({"tool": tool, "sentinel": sentinel})
        assert sentinel is not None
        assert sentinel["risk"] == "sensitive"
        if on_verdict is not None:
            on_verdict("once")
        return None

    monkeypatch.setattr("server.user_control._wait_approval", fake_wait)
    guard = _fresh_guard()
    await guard.acheck("bash", {"command": "rm -rf /tmp/x"}, source="master_tool")
    assert len(calls) == 1
    # "once" stores nothing: the next identical call asks again.
    await guard.acheck("bash", {"command": "rm -rf /tmp/x"}, source="master_tool")
    assert len(calls) == 2


async def test_guard_ask_denied(flag_on, grants, monkeypatch):
    async def fake_wait(tool, kwargs, on_verdict=None, sentinel=None):
        if on_verdict is not None:
            on_verdict("deny")
        return "user denied 'bash'"

    monkeypatch.setattr("server.user_control._wait_approval", fake_wait)
    guard = _fresh_guard()
    with pytest.raises(ToolDenied):
        await guard.acheck("bash", {"command": "rm -rf /tmp/x"}, source="master_tool")


async def test_guard_grant_covers_and_always_persists(flag_on, grants, monkeypatch, tmp_path):
    calls: list[str] = []

    async def fake_wait(tool, kwargs, on_verdict=None, sentinel=None):
        calls.append(tool)
        if on_verdict is not None:
            on_verdict("always")
        return None

    monkeypatch.setattr("server.user_control._wait_approval", fake_wait)
    guard = _fresh_guard()
    await guard.acheck("delete_file", {"path": "/tmp/x"}, source="master_tool")
    assert calls == ["delete_file"]
    # The always grant is recorded: the next call does not ask.
    await guard.acheck("delete_file", {"path": "/tmp/y"}, source="master_tool")
    assert calls == ["delete_file"]
    # And it survives a store reload (persisted scope, same owner scope).
    stored = [g for g in grants._grants if g.key == "delete_file"]
    assert len(stored) == 1 and stored[0].scope == "always"
    reloaded = GrantStore(grants.path)
    assert reloaded.match(
        "delete_file", conversation_id=None, owner_scope=stored[0].owner_scope
    ) is not None


async def test_guard_warnings_skip_grants(flag_on, grants, monkeypatch):
    grants.record(
        key="bash", tool="bash", target=None, scope="always",
        conversation_id=None, owner_scope="",
    )
    calls: list[str] = []

    async def fake_wait(tool, kwargs, on_verdict=None, sentinel=None):
        calls.append(tool)
        if on_verdict is not None:
            on_verdict("once")
        return None

    monkeypatch.setattr("server.user_control._wait_approval", fake_wait)
    guard = _fresh_guard()
    await guard.acheck("bash", {"command": "rm -rf /tmp/x"}, source="master_tool")
    assert calls == ["bash"]


async def test_guard_tainted_egress_asks(flag_on, grants, monkeypatch, taint_key):
    mark_tainted()
    calls: list[str] = []

    async def fake_wait(tool, kwargs, on_verdict=None, sentinel=None):
        calls.append(tool)
        assert sentinel is not None and sentinel["tainted_ask"] is True
        if on_verdict is not None:
            on_verdict("once")
        return None

    monkeypatch.setattr("server.user_control._wait_approval", fake_wait)
    guard = _fresh_guard()
    await guard.acheck("fetch_url", {"url": "https://evil.example/x"}, source="master_tool")
    assert calls == ["fetch_url"]
    # Same call to an allowlisted host does not ask.
    await guard.acheck("fetch_url", {"url": "https://github.com/x"}, source="master_tool")
    assert calls == ["fetch_url"]


async def test_d1_user_control_defers_after_sentinel(flag_on, grants, monkeypatch):
    """Sentinel decided -> user_control opens no second card for the same call."""
    from server import user_control as uc

    calls: list[str] = []

    async def fake_wait(tool, kwargs, on_verdict=None, sentinel=None):
        calls.append(tool)
        if on_verdict is not None:
            on_verdict("once")
        return None

    monkeypatch.setattr("server.user_control._wait_approval", fake_wait)
    monkeypatch.setattr(uc, "require_approval", lambda: True)
    monkeypatch.setattr(uc, "current_mode", lambda: "agent")
    monkeypatch.setattr(uc, "_is_gated_tool", lambda name: True)
    monkeypatch.setenv("VEYA_APPROVAL_GATES", "1")
    monkeypatch.setattr(
        "server.bot_team.sentinel.load_sentinel_config", lambda: SentinelConfig()
    )
    # Without sentinel evaluation, the gated tool reaches the wait.
    result = await uc.user_control_policy("delete_file", {"path": "/tmp/x"}, "master")
    assert result is None
    assert calls == ["delete_file"]
    # After the sentinel policy evaluated (allow), the same call is not asked again.
    guard = _fresh_guard()
    await guard.acheck("write_file", {"path": "/tmp/x"}, source="master_tool")
    calls.clear()
    result = await uc.user_control_policy("delete_file", {"path": "/tmp/x"}, "master")
    assert result is None
    assert calls == []


def test_get_grant_store_singleton_uses_override(grants):
    assert get_grant_store() is grants
