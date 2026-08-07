"""Open Design MCP 接入测试 — stdio(jsonl) 客户端 + 装配层 (真实 daemon, 降级可测)。

覆盖: 握手/工具面/批量适配幂等/降级。
daemon 不可达或 token 缺失时自动跳过 (环境无关)。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from server.open_design import OpenDesignConnector, get_open_design, wire_master_tools


def _od_ready() -> bool:
    env_path = Path(__file__).resolve().parent.parent / "deploy" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("OD_API_TOKEN="):
                os.environ.setdefault("OD_API_TOKEN", line.split("=", 1)[1].strip())
            if line.startswith("OD_DAEMON_URL="):
                os.environ.setdefault("OD_DAEMON_URL", line.split("=", 1)[1].strip())
    bin_path = Path.home() / ".local" / "bin" / "od"
    return bin_path.is_file() and bool(os.environ.get("OD_API_TOKEN"))


HAS_OD = _od_ready()

pytestmark = pytest.mark.skipif(not HAS_OD, reason="od 二进制/token 缺失")


@pytest.fixture()
async def connector():
    c = OpenDesignConnector()
    await c.start()
    yield c
    await c.close()


@pytest.mark.asyncio
async def test_handshake(connector):
    assert connector.ready
    assert connector.health()["available"]


@pytest.mark.asyncio
async def test_tools_list(connector):
    tools = await connector._client.list_tools()
    names = [t["name"] for t in tools]
    assert "list_projects" in names
    assert "get_active_context" in names
    assert "write_file" in names
    assert "create_artifact" in names


@pytest.mark.asyncio
async def test_list_projects_call(connector):
    res = await connector.list_projects()
    assert isinstance(res, list)


@pytest.mark.asyncio
async def test_tool_adapters_namespace(connector):
    adapters = await connector.tool_adapters()
    names = [a["name"] for a in adapters]
    assert "mcp_od_list_projects" in names
    assert all(n.startswith("mcp_od_") for n in names)
    assert len(names) >= 15


@pytest.mark.asyncio
async def test_wire_master_tools_idempotent(connector):
    from server.tool_registry import master_tools

    added = await wire_master_tools(connector)
    assert added >= 15
    assert master_tools.has("mcp_od_write_file")
    assert await wire_master_tools(connector) == 0     # 幂等


def test_singleton():
    assert get_open_design() is get_open_design()


@pytest.mark.asyncio
async def test_missing_token_degrades(monkeypatch):
    monkeypatch.setattr("server.open_design._od_token", lambda: "")
    c = OpenDesignConnector()
    await c.start()                       # token 缺失 → 降级不抛
    assert not c.ready
