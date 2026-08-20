"""Hash-chain egress audit + tool_guard policy."""

from __future__ import annotations

import json

import pytest

from server.egress_audit import (
    destination_of,
    digest_of,
    record_egress,
    sanitize_destination,
    verify_chain,
)
from server.tool_guard import ToolGuard
from server.tool_guard_policies import _EGRESS_POLICY, egress_audit_policy, install_default_tool_policies


def test_sanitize_strips_query_and_userinfo():
    out = sanitize_destination("https://user:pass@example.com/path?token=abc")
    assert out == "https://example.com/path"
    assert "token" not in out
    assert "pass" not in out


def test_destination_only_for_outbound():
    assert destination_of("grep", {"pattern": "x"}) is None
    assert destination_of("fetch_url", {"url": "https://example.com/a"}) == "https://example.com/a"
    assert destination_of("mcp_hevi", {}) == "tool:mcp_hevi"


def test_digest_redacts_secrets():
    d1 = digest_of({"url": "https://x", "api_key": "supersecret"})
    d2 = digest_of({"url": "https://x", "api_key": "other"})
    assert d1 == d2


def test_hash_chain_appends_and_verifies(tmp_path, monkeypatch):
    log = tmp_path / "egress.jsonl"
    monkeypatch.setenv("VEYA_EGRESS_LOG", str(log))
    a = record_egress(tool="fetch_url", destination="https://a.example/", digest="d1", owner_id="u1")
    b = record_egress(tool="fetch_url", destination="https://b.example/", digest="d2", owner_id="u1")
    assert a["prev"] == "0" * 64
    assert b["prev"] == a["hash"]
    ok, reason = verify_chain(log)
    assert ok is True, reason
    # tamper
    lines = log.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["payload"]["destination"] = "https://evil.example/"
    lines[0] = json.dumps(rec)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, reason = verify_chain(log)
    assert ok is False
    assert "hash mismatch" in reason


def test_policy_records_then_allowlists(tmp_path, monkeypatch):
    log = tmp_path / "egress.jsonl"
    monkeypatch.setenv("VEYA_EGRESS_LOG", str(log))
    monkeypatch.setenv("VEYA_EGRESS_ENFORCE", "1")
    monkeypatch.setenv("VEYA_EGRESS_ALLOWLIST", "allowed.example")
    assert (
        egress_audit_policy("fetch_url", {"url": "https://allowed.example/x"}, "test")
        is None
    )
    deny = egress_audit_policy("fetch_url", {"url": "https://evil.example/x"}, "test")
    assert deny is not None and "egress denied" in deny
    ok, _ = verify_chain(log)
    assert ok is True
    assert log.read_text(encoding="utf-8").count("\n") == 2


def test_install_registers_egress(monkeypatch):
    monkeypatch.delenv("VEYA_EGRESS_ENFORCE", raising=False)
    g = ToolGuard()
    install_default_tool_policies(g)
    assert g.has_policy(_EGRESS_POLICY)
    install_default_tool_policies(g)
    assert g.policy_names.count(_EGRESS_POLICY) == 1
