"""Persistent publication permissions and real ToolGuard execution-path tests."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from server import events as events_mod
from server.app import app
from server.events import EventStore
from server.tool_registry import MasterToolRegistry, ToolExecutionError
from veya.tdx.publish_permissions import check_publish_permission, load_permissions

client = TestClient(app)


def _test_stores(monkeypatch, tmp_path):
    monkeypatch.setenv("VEYA_TDX_PUBLISH_PERMS_PATH", str(tmp_path / "publish-perms.json"))
    monkeypatch.setattr(events_mod, "event_store", EventStore(tmp_path / "events.jsonl"))


def test_publish_perms_flag_off(monkeypatch, owner_headers):
    monkeypatch.delenv("VEYA_TDX_AGENT_PUBLISH_PERMS", raising=False)
    monkeypatch.setenv("VEYA_OWNER_USER_ID", "owner-123")
    resp = client.get("/api/v1/tdx/publish-perms", headers=owner_headers)
    assert resp.status_code == 404


def test_publish_perms_are_persisted_and_audited(monkeypatch, tmp_path, owner_headers):
    _test_stores(monkeypatch, tmp_path)
    monkeypatch.setenv("VEYA_TDX_AGENT_PUBLISH_PERMS", "true")
    monkeypatch.setenv("VEYA_OWNER_USER_ID", "owner-123")

    resp = client.get("/api/v1/tdx/publish-perms", headers=owner_headers)
    assert resp.status_code == 200
    assert resp.json()["permissions"]["default"] == {"push": False, "merge": False, "tag": False, "deploy": False}

    resp_put = client.put(
        "/api/v1/tdx/publish-perms/agent-x",
        json={"push": True, "merge": False, "tag": False, "deploy": False},
        headers=owner_headers,
    )
    assert resp_put.status_code == 200
    assert resp_put.json()["permissions"]["push"] is True
    # Reload from disk to prove the state is not merely held in process memory.
    assert load_permissions()["agent-x"]["push"] is True

    check_publish_permission("agent-x", "push")
    with pytest.raises(PermissionError):
        check_publish_permission("agent-x", "merge")

    events = events_mod.event_store.read_all(topic_prefixes=("tdx.publish_perms_updated", "tdx.publish_allowed", "tdx.publish_denied"))
    assert any(e.get("topic") == "tdx.publish_denied" and e.get("payload", {}).get("action") == "merge" for e in events)
    assert any(e.get("topic") == "tdx.publish_allowed" and e.get("payload", {}).get("action") == "push" for e in events)


def test_tool_guard_blocks_physical_git_push_and_writes_denial_event(monkeypatch, tmp_path):
    _test_stores(monkeypatch, tmp_path)
    # main's terminal_action_gate already denies git_push; allowlist it here so the
    # TDX publication policy is the gate under test (defense in depth, see below).
    monkeypatch.setenv("VEYA_TOOL_GATE_ALLOWLIST", "git_push")
    monkeypatch.setenv("VEYA_TDX_AGENT_PUBLISH_PERMS", "true")
    monkeypatch.delenv("VEYA_OWNER_USER_ID", raising=False)
    called = []

    async def physical_push(agent_id: str):
        called.append(agent_id)
        return "pushed"

    registry = MasterToolRegistry(timeout_s=5)
    registry.register(
        "git_push",
        "Push a branch to a remote.",
        {"type": "object", "properties": {"agent_id": {"type": "string"}, "remote": {"type": "string"}}, "required": ["agent_id"]},
        physical_push,
    )
    with pytest.raises(ToolExecutionError, match="TDX publication policy blocked"):
        asyncio.run(registry.execute("git_push", {"agent_id": "agent-ungranted", "remote": "origin/main"}))

    assert called == []
    denied = events_mod.event_store.read_all(topic_prefixes=("tdx.publish_denied",))
    assert any(
        e.get("payload", {}).get("agent_id") == "agent-ungranted"
        and e.get("payload", {}).get("action") == "push"
        and e.get("payload", {}).get("target") == "origin/main"
        for e in denied
    )


def test_tool_guard_flag_off_preserves_original_execution(monkeypatch, tmp_path):
    _test_stores(monkeypatch, tmp_path)
    monkeypatch.setenv("VEYA_TOOL_GATE_ALLOWLIST", "git_push")
    monkeypatch.delenv("VEYA_TDX_AGENT_PUBLISH_PERMS", raising=False)
    called = []

    async def physical_push(agent_id: str):
        called.append(agent_id)
        return "pushed"

    registry = MasterToolRegistry(timeout_s=5)
    registry.register(
        "git_push",
        "Push a branch to a remote.",
        {"type": "object", "properties": {"agent_id": {"type": "string"}}, "required": ["agent_id"]},
        physical_push,
    )
    assert asyncio.run(registry.execute("git_push", {"agent_id": "agent-ungranted"})) == "pushed"
    assert called == ["agent-ungranted"]


def test_terminal_gate_still_blocks_git_push_without_tdx(monkeypatch, tmp_path):
    """With TDX off and no allowlist, main's terminal_action_gate still denies git_push."""
    _test_stores(monkeypatch, tmp_path)
    monkeypatch.delenv("VEYA_TDX_AGENT_PUBLISH_PERMS", raising=False)
    monkeypatch.delenv("VEYA_TOOL_GATE_ALLOWLIST", raising=False)
    called = []

    async def physical_push(agent_id: str):
        called.append(agent_id)
        return "pushed"

    registry = MasterToolRegistry(timeout_s=5)
    registry.register(
        "git_push",
        "Push a branch to a remote.",
        {"type": "object", "properties": {"agent_id": {"type": "string"}}, "required": ["agent_id"]},
        physical_push,
    )
    with pytest.raises(ToolExecutionError, match="terminal_action_gate"):
        asyncio.run(registry.execute("git_push", {"agent_id": "agent-ungranted"}))
    assert called == []
