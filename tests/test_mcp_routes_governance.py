"""Legacy MCP REST endpoints remain on the canonical governance path."""

from __future__ import annotations

import inspect
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from server import events as events_module
from server.events import EventStore
from server.routes import mcp


class _RouteMcpClient:
    def __init__(self, *, failure: bool = False) -> None:
        self.failure = failure
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.connect_calls = 0

    async def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "search",
                "description": "fixture search",
                "inputSchema": {"type": "object"},
                "effect": "read",
            },
            {
                "name": "publish",
                "description": "fixture publish",
                "inputSchema": {"type": "object"},
                "effect": "remote",
            },
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        if self.failure:
            raise RuntimeError(f"fixture transport failed {uuid.uuid4().hex}")
        return {"tool": name, "args": args}


def _grant(tool: str, effect: str, *, revoked: bool = False) -> dict[str, Any]:
    return {
        "tool": tool,
        "subject": "master",
        "allowed_effects": [effect],
        "tool_version": "1",
        "resource": "*",
        "revoked": revoked,
    }


@pytest.fixture
def route_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = _RouteMcpClient()
    audit_store = EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr(events_module, "event_store", audit_store)
    monkeypatch.setenv("VEYA_EXECUTION_SQLITE_PATH", str(tmp_path / "execution.sqlite3"))
    monkeypatch.setenv("VEYA_OUTPUT_DIR", str(tmp_path / "runs"))

    async def connect(_config: dict[str, Any], **_kwargs: Any) -> _RouteMcpClient:
        client.connect_calls += 1
        return client

    monkeypatch.setattr("veya.compat.mcp_connect", connect)
    monkeypatch.setattr(
        "server.user_control.request_approval",
        lambda *_args, **_kwargs: _async_return(True),
    )
    mcp._registered.clear()
    yield client, audit_store
    mcp._registered.clear()


async def _async_return(value: Any) -> Any:
    return value


async def _connect(client: _RouteMcpClient, *, grant: dict[str, Any] | None = None) -> None:
    del client
    await mcp.mcp_connect_route(
        mcp.MCPConnectRequest(
            name="fixture",
            url="https://fixture.invalid/mcp",
            grant=grant or _grant("native/mcp_connect@1", "network"),
        )
    )


@pytest.mark.asyncio
async def test_mcp_rest_allow_keeps_legacy_response_and_audit(
    route_fixture: tuple[_RouteMcpClient, EventStore],
) -> None:
    client, audit_store = route_fixture
    await _connect(client)
    result = await mcp.mcp_call_route(
        mcp.MCPCallRequest(
            server="fixture",
            tool="search",
            args={"query": "authority"},
            grant=_grant("mcp/fixture/search@1", "read"),
        )
    )

    assert result == {
        "status": "success",
        "result": {"tool": "search", "args": {"query": "authority"}},
    }
    assert client.calls == [("search", {"query": "authority"})]
    audits = audit_store.read_all(topics={"action_gateway.audit"})
    assert len(audits) >= 6  # connect and call each emit govern/execute/complete


@pytest.mark.asyncio
async def test_mcp_rest_deny_and_missing_grant_have_zero_transport_effect(
    route_fixture: tuple[_RouteMcpClient, EventStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _audit_store = route_fixture
    await _connect(client)

    # The development profile asks for this remote action; a negative answer
    # becomes the canonical DENY and must still stop before transport.
    monkeypatch.setattr(
        "server.user_control.request_approval",
        lambda *_args, **_kwargs: _async_return(False),
    )
    with pytest.raises(Exception) as denied:
        await mcp.mcp_call_route(
            mcp.MCPCallRequest(
                server="fixture",
                tool="publish",
                grant=_grant("mcp/fixture/publish@1", "remote"),
            )
        )
    assert getattr(denied.value, "status_code", None) == 403

    with pytest.raises(Exception) as missing:
        await mcp.mcp_call_route(mcp.MCPCallRequest(server="fixture", tool="search"))
    assert getattr(missing.value, "status_code", None) == 403
    assert client.calls == []
    assert client.connect_calls == 1


@pytest.mark.asyncio
async def test_mcp_rest_approval_and_stale_grant_fail_closed(
    route_fixture: tuple[_RouteMcpClient, EventStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _audit_store = route_fixture
    await _connect(client)

    approval_requests: list[str] = []

    async def approve(action: str, _args: dict[str, Any]) -> bool:
        approval_requests.append(action)
        return True

    monkeypatch.setattr("server.user_control.request_approval", approve)
    result = await mcp.mcp_call_route(
        mcp.MCPCallRequest(
            server="fixture",
            tool="publish",
            grant=_grant("mcp/fixture/publish@1", "remote"),
        )
    )
    assert result["status"] == "success"
    assert approval_requests == ["publish"]

    before = list(client.calls)
    with pytest.raises(Exception) as stale:
        await mcp.mcp_call_route(
            mcp.MCPCallRequest(
                server="fixture",
                tool="publish",
                grant=_grant("mcp/fixture/publish@1", "remote", revoked=True),
            )
        )
    assert getattr(stale.value, "status_code", None) == 403
    assert client.calls == before


@pytest.mark.asyncio
async def test_mcp_rest_transport_failure_and_result_redaction(
    route_fixture: tuple[_RouteMcpClient, EventStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _audit_store = route_fixture
    await _connect(client)
    client.failure = True

    with pytest.raises(Exception) as failed:
        await mcp.mcp_call_route(
            mcp.MCPCallRequest(
                server="fixture",
                tool="search",
                grant=_grant("mcp/fixture/search@1", "read"),
            )
        )
    assert getattr(failed.value, "status_code", None) == 502
    assert "fixture transport failed" not in str(failed.value)

    secret_value = f"runtime-{uuid.uuid4().hex}"
    client.failure = False

    async def redacting_call(name: str, args: dict[str, Any]) -> Any:
        client.calls.append((name, args))
        return {"token": secret_value, "ok": True}

    monkeypatch.setattr(client, "call_tool", redacting_call)
    result = await mcp.mcp_call_route(
        mcp.MCPCallRequest(
            server="fixture",
            tool="search",
            grant=_grant("mcp/fixture/search@1", "read"),
        )
    )
    assert secret_value not in json.dumps(result)
    assert result["result"]["token"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_mcp_rest_missing_credential_fails_before_transport(
    route_fixture: tuple[_RouteMcpClient, EventStore],
) -> None:
    client, _audit_store = route_fixture
    await mcp.mcp_connect_route(
        mcp.MCPConnectRequest(
            name="credential-fixture",
            url="https://fixture.invalid/mcp",
            credential_ref={"type": "credential_ref", "id": "fixture-ref"},
            grant=_grant("native/mcp_connect@1", "network"),
        )
    )
    with pytest.raises(Exception) as missing:
        await mcp.mcp_call_route(
            mcp.MCPCallRequest(
                server="credential-fixture",
                tool="search",
                grant=_grant("mcp/credential-fixture/search@1", "read"),
            )
        )
    assert getattr(missing.value, "status_code", None) == 403
    assert client.calls == []


@pytest.mark.asyncio
async def test_mcp_rest_connect_missing_grant_has_zero_external_effect(
    route_fixture: tuple[_RouteMcpClient, EventStore],
) -> None:
    client, _audit_store = route_fixture
    with pytest.raises(Exception) as denied:
        await mcp.mcp_connect_route(
            mcp.MCPConnectRequest(name="not-authorized", url="https://fixture.invalid/mcp")
        )
    assert getattr(denied.value, "status_code", None) == 403
    assert client.calls == []
    assert client.connect_calls == 0


@pytest.mark.asyncio
async def test_mcp_rest_audit_redacts_url_userinfo(
    route_fixture: tuple[_RouteMcpClient, EventStore],
) -> None:
    _client, audit_store = route_fixture
    runtime_secret = f"runtime-{uuid.uuid4().hex}"
    await mcp.mcp_connect_route(
        mcp.MCPConnectRequest(
            name="userinfo-fixture",
            url=f"https://user:{runtime_secret}@fixture.invalid/mcp",
            grant=_grant("native/mcp_connect@1", "network"),
        )
    )
    serialized = json.dumps(audit_store.read_all(topics={"action_gateway.audit"}))
    assert runtime_secret not in serialized


def test_mcp_rest_module_has_no_direct_tool_transport_call() -> None:
    source = inspect.getsource(mcp)
    assert "mcp_call_tool" not in source
    assert "ToolGovernanceAdapter" in source
    assert "entry.adapter.execute" in source
