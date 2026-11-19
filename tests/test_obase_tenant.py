"""Server-only tenant context: frozen identity, host resolution, fail-closed."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from veya.obase import audit_chain as ac
from veya.obase import tenant as t


def _scope(host: str = "localhost") -> dict:
    return {"type": "http", "path": "/api/v1/mcp/health", "headers": [(b"host", host.encode())]}


def test_client_cannot_construct_tenant_context():
    with pytest.raises(t.TenantResolutionError):
        t.TenantContext(tenant_id="attacker", _server_token=object())
    with pytest.raises(TypeError):
        t.TenantContext(**{"tenant_id": "attacker"})  # no server token at all
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.current_tenant().tenant_id = "attacker"


def test_default_single_tenant_without_mapping():
    ctx = t.resolve_tenant_context(_scope(), {"user_id": "u1"}, env={})
    assert ctx.tenant_id == t.DEFAULT_TENANT
    assert t.current_tenant().tenant_id == t.DEFAULT_TENANT


def test_host_and_user_mappings(monkeypatch):
    env = {
        "VEYA_TENANT_HOST_MAP": "TeamA.example.com=team-a",
        "VEYA_TENANT_USER_MAP": "u1=team-user",
    }
    assert (
        t.resolve_tenant_context(
            _scope("teama.example.com:443"), {"user_id": "u1"}, env=env
        ).tenant_id
        == "team-a"
    )
    assert (
        t.resolve_tenant_context(_scope("unknown.example"), {"user_id": "U1"}, env=env).tenant_id
        == "team-user"
    )


def test_strict_mode_fails_closed_on_unknown_or_conflicting_inputs():
    strict = {"VEYA_TENANT_STRICT": "1"}
    with pytest.raises(t.TenantResolutionError):
        t.resolve_tenant_context(_scope("unknown.example"), {"user_id": "u1"}, env=strict)
    # A mapped Host is sufficient; authenticated user is not required.
    assert (
        t.resolve_tenant_context(
            _scope("a.example"), None, env={**strict, "VEYA_TENANT_HOST_MAP": "a.example=a"}
        ).tenant_id
        == "a"
    )
    conflict = {
        **strict,
        "VEYA_TENANT_HOST_MAP": "a.example=a",
        "VEYA_TENANT_USER_MAP": "u1=b",
    }
    with pytest.raises(t.TenantResolutionError):
        t.resolve_tenant_context(_scope("a.example"), {"user_id": "u1"}, env=conflict)


@pytest.mark.asyncio
async def test_contextvar_flows_into_spawned_tasks_and_audit_writes():
    import sqlite3

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    store = ac.AuditChainStore(conn)
    prev = ac.configure_audit_chain_store(store)
    ctx = t.resolve_tenant_context(
        _scope("a.example"),
        {"user_id": "u1"},
        env={"VEYA_TENANT_HOST_MAP": "a.example=tenant-a"},
    )
    token = t.bind_tenant_context(ctx)
    try:
        assert (
            await asyncio.create_task(asyncio.to_thread(lambda: t.current_tenant().tenant_id))
            == "tenant-a"
        )
        ac.safe_append(action="risk.asked", detail={"n": 1})
        assert store.head("tenant-a")["seq"] == 1
        assert store.head(t.DEFAULT_TENANT) is None
    finally:
        t.reset_tenant_context(token)
        ac.configure_audit_chain_store(prev)
        store.close()


@pytest.mark.asyncio
async def test_auth_gate_binds_tenant_and_strict_returns_403(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server import auth

    seen = []

    app = FastAPI()

    @app.get("/api/v1/mcp/health")
    def health():
        seen.append(t.current_tenant().tenant_id)
        return {"ok": True}

    app.add_middleware(auth.AuthGateMiddleware)
    monkeypatch.setenv("VEYA_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("VEYA_TENANT_STRICT", "0")
    monkeypatch.setenv("VEYA_TENANT_HOST_MAP", "a.example=tenant-a")

    client = TestClient(app, client=("127.0.0.1", 12345))
    assert client.get("/api/v1/mcp/health", headers={"Host": "a.example"}).status_code == 200
    assert seen[-1] == "tenant-a"
    assert t.current_tenant().tenant_id == t.DEFAULT_TENANT  # context reset after request

    monkeypatch.setenv("VEYA_TENANT_STRICT", "1")
    response = client.get("/api/v1/mcp/health", headers={"Host": "unknown.example"})
    assert response.status_code == 403
    assert not seen or seen[-1] == "tenant-a"
