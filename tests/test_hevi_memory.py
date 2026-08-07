"""hevi MCP 接入测试 — HTTP MCP 客户端 + 装配层 (真实 hevi-api, 降级可测)。

覆盖: JWT 签发、握手/伪造头、工具面、批量适配幂等、密钥缺失降级。
hevi-api 不可达或 HEVI_JWT_SECRET 缺失时自动跳过 (环境无关)。
"""

from __future__ import annotations

import pytest

from server.hevi_memory import HeviConnector, _sign_hevi_jwt, get_hevi, wire_master_tools


def _hevi_reachable() -> bool:
    import os
    import socket
    from pathlib import Path

    # 测试环境: 从 deploy/.env 读 HEVI_JWT_SECRET (compose 用)
    env_path = Path(__file__).resolve().parent.parent / "deploy" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("HEVI_JWT_SECRET="):
                os.environ.setdefault("HEVI_JWT_SECRET", line.split("=", 1)[1].strip())
            if line.startswith("HEVI_MCP_USER_ID="):
                os.environ.setdefault("HEVI_MCP_USER_ID", line.split("=", 1)[1].strip())
    for host, port in (("127.0.0.1", 8201), ("hevi-cftunnel-hevi-api-1", 8000)):
        try:
            with socket.create_connection((host, port), timeout=1.5):
                return True
        except OSError:
            continue
    return False


HAS_HEVI = _hevi_reachable() and bool(_sign_hevi_jwt())

pytestmark = pytest.mark.skipif(not HAS_HEVI, reason="hevi-api 不可达或密钥缺失")


@pytest.fixture()
async def connector():
    c = HeviConnector(endpoint="http://127.0.0.1:8201/mcp/mcp")
    await c.start()
    yield c
    await c.close()


def test_jwt_signing():
    token = _sign_hevi_jwt()
    assert token and len(token) > 50


@pytest.mark.asyncio
async def test_handshake(connector):
    assert connector.ready
    assert connector.health()["available"]


@pytest.mark.asyncio
async def test_tools_list(connector):
    tools = await connector._client.list_tools()
    names = [t["name"] for t in tools]
    assert "hevi.generate_longvideo" in names
    assert "hevi.gen_storyboard" in names
    assert "hevi.list_capabilities" in names


@pytest.mark.asyncio
async def test_generate_longvideo_call(connector):
    res = await connector.generate_longvideo("测试主题", duration="short")
    assert isinstance(res, dict)          # 任务创建链路通 (可能返回任务 id 或错误详情)


@pytest.mark.asyncio
async def test_storyboard_call(connector):
    # hevi 侧 LLM provider 未配置时工具返回错误 — 链路通到工具层即可
    from obase.mcp_http import HttpMcpError

    try:
        res = await connector.gen_storyboard("司马光砸缸", shots=4)
        assert isinstance(res, dict)
    except HttpMcpError as exc:
        assert "gen_storyboard" in str(exc)


@pytest.mark.asyncio
async def test_tool_adapters_namespace(connector):
    adapters = await connector.tool_adapters()
    names = [a["name"] for a in adapters]
    assert "mcp_hevi_hevi_generate_longvideo" in names
    assert all(n.startswith("mcp_hevi_") for n in names)


@pytest.mark.asyncio
async def test_wire_master_tools_idempotent(connector):
    from server.tool_registry import master_tools

    added = await wire_master_tools(connector)
    assert added >= 10
    assert master_tools.has("mcp_hevi_hevi_generate_longvideo")
    assert await wire_master_tools(connector) == 0     # 幂等


def test_singleton():
    assert get_hevi() is get_hevi()


@pytest.mark.asyncio
async def test_missing_secret_degrades(monkeypatch):
    monkeypatch.setattr("server.hevi_memory._hevi_secret", lambda: "")
    c = HeviConnector()
    await c.start()                       # 密钥缺失 → 降级不抛
    assert not c.ready
