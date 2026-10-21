"""Phase 0 world-intel-mcp integration tests (no real process or network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server import lazy_tools, world_intel_mcp
from server.tool_registry import (
    MasterToolRegistry,
    SideEffect,
    _current_master_session,
    master_tools,
    register_lazy_search_tool,
)


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def start(self) -> None:
        return None

    async def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "intel_earthquakes",
                "description": "Get recent earthquakes from USGS. No API key needed.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"min_magnitude": {"type": "number"}},
                    "required": [],
                },
            },
            {
                "name": "intel_market_quotes",
                "description": "Get real-time stock market index quotes.",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        return {"content": [{"type": "text", "text": "ok"}]}

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(world_intel_mcp.FLAG_NAME, raising=False)
    monkeypatch.delenv(world_intel_mcp.ROOT_ENV, raising=False)
    lazy_tools.reset_sessions()
    yield
    lazy_tools.reset_sessions()


def _schema_bytes(schemas: list[dict[str, Any]]) -> bytes:
    return json.dumps(schemas, ensure_ascii=False, separators=(",", ":")).encode()


async def test_flag_off_global_surface_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off is a no-op: no spawn and no change to the canonical surface."""
    monkeypatch.setattr(
        world_intel_mcp,
        "_build_client",
        lambda _root: (_ for _ in ()).throw(AssertionError("spawned while disabled")),
    )
    before = _schema_bytes(master_tools.get_all_schemas())

    assert world_intel_mcp.status() == {
        "name": world_intel_mcp.SERVER_NAME,
        "available": False,
        "reason": world_intel_mcp.UNAVAILABLE_FLAG,
    }
    assert await world_intel_mcp.wire_master_tools() == 0
    assert _schema_bytes(master_tools.get_all_schemas()) == before
    assert not master_tools.has(world_intel_mcp.GATEWAY_NAME)


async def test_flag_off_injected_registry_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        world_intel_mcp,
        "_build_client",
        lambda _root: (_ for _ in ()).throw(AssertionError("spawned while disabled")),
    )
    registry = MasterToolRegistry()
    registry.register("alpha", "alpha", {"type": "object", "properties": {}}, lambda: "ok")
    before = _schema_bytes(registry.get_all_schemas())

    assert await world_intel_mcp.wire_master_tools(registry=registry) == 0
    assert _schema_bytes(registry.get_all_schemas()) == before


async def test_flag_on_registers_one_lazy_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(world_intel_mcp.FLAG_NAME, "1")
    monkeypatch.setenv(world_intel_mcp.ROOT_ENV, str(tmp_path))
    registry = MasterToolRegistry()
    registry.register("alpha", "alpha", {"type": "object", "properties": {}}, lambda: "ok")
    register_lazy_search_tool(registry)
    client = _FakeClient()

    assert (
        await world_intel_mcp.wire_master_tools(
            registry=registry, client_factory=lambda _root: client
        )
        == 1
    )
    assert registry.has(world_intel_mcp.GATEWAY_NAME)
    assert world_intel_mcp.GATEWAY_NAME not in lazy_tools.resident_tool_names()
    assert registry.spec_for(world_intel_mcp.GATEWAY_NAME).side_effect is SideEffect.PURE_READ

    description = registry.describe(world_intel_mcp.GATEWAY_NAME)
    assert "mcp_world_intel_earthquakes" in description
    assert "mcp_world_intel_market_quotes" in description
    assert world_intel_mcp.UNTRUSTED_NOTE in description

    session = "world-intel-lazy"
    token = _current_master_session.set(session)
    catalog = lazy_tools.bind_catalog(registry.get_all_schemas())
    try:
        payload = json.loads(
            await registry.execute("search_tools", {"query": "earthquakes", "top_k": 3})
        )
        assert world_intel_mcp.GATEWAY_NAME in payload["loaded"]
        assert world_intel_mcp.GATEWAY_NAME in {
            schema["function"]["name"]
            for schema in lazy_tools.select_schemas(
                registry.get_all_schemas(),
                session_id=session,
                resident_names=("alpha",),
            )
        }
    finally:
        lazy_tools.reset_catalog(catalog)
        _current_master_session.reset(token)
        await world_intel_mcp.close_world_intel()

    assert client.closed is True
    assert client.calls == []


async def test_gateway_call_preserves_upstream_tool_name_and_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(world_intel_mcp.FLAG_NAME, "1")
    monkeypatch.setenv(world_intel_mcp.ROOT_ENV, str(tmp_path))
    registry = MasterToolRegistry()
    client = _FakeClient()

    assert (
        await world_intel_mcp.wire_master_tools(
            registry=registry, client_factory=lambda _root: client
        )
        == 1
    )
    try:
        result = await registry.execute(
            world_intel_mcp.GATEWAY_NAME,
            {
                "action": "mcp_world_intel_earthquakes",
                "args": {"min_magnitude": 5},
            },
        )
    finally:
        await world_intel_mcp.close_world_intel()

    assert "ok" in result
    assert client.calls == [("intel_earthquakes", {"min_magnitude": 5})]


async def test_missing_root_degrades_without_spawning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(world_intel_mcp.FLAG_NAME, "1")
    missing = tmp_path / "missing"
    monkeypatch.setenv(world_intel_mcp.ROOT_ENV, str(missing))
    registry = MasterToolRegistry()

    assert world_intel_mcp.status() == {
        "name": world_intel_mcp.SERVER_NAME,
        "available": False,
        "reason": f"unavailable: root missing ({missing})",
    }
    assert (
        await world_intel_mcp.wire_master_tools(
            registry=registry,
            client_factory=lambda _root: (_ for _ in ()).throw(AssertionError("spawned")),
        )
        == 0
    )
    assert not registry.has(world_intel_mcp.GATEWAY_NAME)


async def test_startup_failure_degrades_and_closes_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(world_intel_mcp.FLAG_NAME, "1")
    monkeypatch.setenv(world_intel_mcp.ROOT_ENV, str(tmp_path))
    registry = MasterToolRegistry()

    class _BrokenClient(_FakeClient):
        async def start(self) -> None:
            raise RuntimeError("upstream unavailable")

    client = _BrokenClient()
    assert (
        await world_intel_mcp.wire_master_tools(
            registry=registry, client_factory=lambda _root: client
        )
        == 0
    )
    assert not registry.has(world_intel_mcp.GATEWAY_NAME)
    assert client.closed is True


def test_command_uses_module_entry_not_host_venv_script(tmp_path: Path) -> None:
    """The host .venv script cannot run in the container; always use -m.

    A checkout with an executable .venv/bin/world-intel-mcp must still launch
    the module under the current interpreter (sys.executable), because the
    script's shebang points at the host venv.
    """
    import sys

    script = tmp_path / ".venv" / "bin" / "world-intel-mcp"
    script.parent.mkdir(parents=True)
    script.write_text("#!/host/venv/bin/python\n")
    script.chmod(0o755)

    command = world_intel_mcp._command(tmp_path)
    assert command == [sys.executable, "-m", "world_intel_mcp.server"]
    assert str(script) not in command


async def test_wire_failure_logs_exception_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(world_intel_mcp.FLAG_NAME, "1")
    monkeypatch.setenv(world_intel_mcp.ROOT_ENV, str(tmp_path))
    registry = MasterToolRegistry()

    class _BrokenClient(_FakeClient):
        async def start(self) -> None:
            raise RuntimeError("upstream unavailable")

    with caplog.at_level("WARNING", logger="veya.world_intel_mcp"):
        assert (
            await world_intel_mcp.wire_master_tools(
                registry=registry, client_factory=lambda _root: _BrokenClient()
            )
            == 0
        )
    record = next(r for r in caplog.records if "not wired" in r.getMessage())
    assert record.exc_info is not None
    assert "upstream unavailable" in record.getMessage()
