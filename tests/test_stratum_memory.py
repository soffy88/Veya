"""Stratum MCP 接入测试 — HTTP MCP 客户端 + 装配层 (真实 stratum-api, 降级可测)。

覆盖: 握手/session、工具清单、call_tool、双通道装配幂等、不可达降级。
stratum-api 不可达时自动跳过 (环境无关)。
"""

from __future__ import annotations

import pytest

from server.stratum_memory import StratumConnector, get_stratum, wire_master_tools


def _endpoint_reachable() -> bool:
    import socket

    # 宿主测试进程: 走 docker 映射端口 (9309→stratum-api:9302)
    for host, port in (("127.0.0.1", 9309), ("stratum-api", 9302)):
        try:
            with socket.create_connection((host, port), timeout=1.5):
                return True
        except OSError:
            continue
    return False


HAS_STRATUM = _endpoint_reachable()

pytestmark = pytest.mark.skipif(not HAS_STRATUM, reason="stratum-api 不可达")


@pytest.fixture()
async def connector():
    c = StratumConnector(endpoint="http://127.0.0.1:9309/mcp/mcp")  # 宿主映射端口
    await c.start()
    yield c
    await c.close()


@pytest.mark.asyncio
async def test_handshake_and_session(connector):
    assert connector.ready
    h = connector.health()
    assert h["available"] and h["session"]


@pytest.mark.asyncio
async def test_tools_list(connector):
    tools = await connector._client.list_tools()
    names = [t["name"] for t in tools]
    assert "search_knowledge" in names
    assert "list_recent_notes" in names
    assert "retrieve_context" in names


@pytest.mark.asyncio
async def test_search_knowledge_call(connector):
    res = await connector.search_knowledge("test", top_k=3)
    # MCP call_tool 返回信封 dict (content[].text = JSON 字符串)
    assert isinstance(res, dict) and "content" in res
    assert res.get("isError") is False


@pytest.mark.asyncio
async def test_tool_adapters_namespace(connector):
    adapters = await connector.tool_adapters()
    names = [a["name"] for a in adapters]
    assert "mcp_stratum_search_knowledge" in names
    assert all(n.startswith("mcp_stratum_") for n in names)


@pytest.mark.asyncio
async def test_wire_master_tools_idempotent(connector):
    from server.tool_registry import master_tools

    added = await wire_master_tools(connector)
    # ②-B 网关: 每服务 1 个 mcp_<server> 网关 (全量顺序下可能已 wire → added=0)
    assert added >= 0
    assert master_tools.has("mcp_stratum")
    assert await wire_master_tools(connector) == 0  # 幂等


def test_singleton():
    assert get_stratum() is get_stratum()


@pytest.mark.asyncio
async def test_unreachable_degrades():
    c = StratumConnector(endpoint="http://no-such-host:1/mcp")
    await c.start()  # 降级不抛
    assert not c.ready
    assert c.health()["available"] is False
