"""server/stratum_memory.py — Stratum 知识库 MCP 接入装配层。

3O 铁律: 机制 (HTTP MCP 客户端) 在主库 obase.mcp_http; 本模块只装配:
  - StreamableHttpMcpClient 连接 stratum-api /mcp → McpClientRegistry.register("stratum");
  - 工具批量 make_mcp_tool_adapter → mcp_stratum_* 进 master_tools (主脑知识面);
  - 优雅降级: stratum 不可达时跳过, 不阻塞服务。

Stratum (同宿主机, 同 docker 网络) 是 veya 的"知识智能层" — 概念图谱/
笔记/记忆检索, 与 codebase-memory (代码智能) 互补。
"""

from __future__ import annotations

import os
from typing import Any

STRATUM_MCP_ENDPOINT = os.environ.get(
    "STRATUM_MCP_ENDPOINT", "http://stratum-api:9302/mcp/mcp")


class StratumConnector:
    """Stratum MCP 装配器 (单例: app lifespan 启动)。"""

    def __init__(self, endpoint: str | None = None, name: str = "stratum") -> None:
        self.endpoint = endpoint or STRATUM_MCP_ENDPOINT
        self.name = name
        self._client: Any = None

    @property
    def ready(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "_started", False))

    async def start(self) -> None:
        """握手 + 注册。失败降级 (不抛)。"""
        if self.ready:
            return
        from veya.platform import obase as _load_obase

        _obase = _load_obase()
        McpClientRegistry = _obase.McpClientRegistry
        StreamableHttpMcpClient = _obase.StreamableHttpMcpClient

        try:
            client = StreamableHttpMcpClient(self.endpoint, name=self.name, timeout=30.0)
            await client.start()
        except Exception:
            return      # stratum 不可达 → 降级
        self._client = client
        McpClientRegistry.register(self.name, client)

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    def health(self) -> dict[str, Any]:
        if not self._client:
            return {"available": False, "reason": "未连接"}
        return {"available": True, **self._client.health()}

    # ── 知识查询 (供 coordinator/路由直接调用) ─────────────────────────

    async def search_knowledge(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        """跨层检索 (BM25+向量+rerank) — Stratum 知识库。"""
        res = await self._client.call_tool("search_knowledge",
                                           {"query_text": query, "top_k": top_k})
        return (res or {}).get("results", [])

    async def get_note(self, note_id: str) -> dict[str, Any] | None:
        res = await self._client.call_tool("get_note", {"note_id": note_id})
        return res if isinstance(res, dict) else None

    async def list_recent_notes(self, limit: int = 10) -> list[dict[str, Any]]:
        res = await self._client.call_tool("list_recent_notes", {"limit": limit})
        return res if isinstance(res, list) else []

    # ── LLM 工具面 ─────────────────────────────────────────────────────

    async def tool_adapters(self) -> list[dict[str, Any]]:
        """批量适配 → master_tools 可注册 (name/desc/params/func)。"""
        if not self.ready:
            return []
        from tools import make_mcp_tool_adapter

        out = []
        for spec in await self._client.list_tools():
            adapter = make_mcp_tool_adapter(spec, self._client)
            params = (spec.get("inputSchema") or {}).get("properties", {})
            required = (spec.get("inputSchema") or {}).get("required", [])
            out.append({
                "name": f"mcp_stratum_{spec['name']}",
                "description": adapter.description,
                "parameters": {"type": "object", "properties": params, "required": required},
                "func": adapter.callable,
            })
        return out


async def wire_master_tools(connector: StratumConnector | None = None) -> int:
    """mcp_stratum_* 注册进 master_tools (主脑知识面), 幂等。"""
    from server.tool_registry import master_tools

    connector = connector or get_stratum()
    added = 0
    for a in await connector.tool_adapters():
        if master_tools.has(a["name"]):
            continue
        master_tools.register(a["name"], a["description"], a["parameters"], a["func"],
                              max_result_chars=16000)
        added += 1
    return added


_stratum: StratumConnector | None = None


def get_stratum() -> StratumConnector:
    global _stratum
    if _stratum is None:
        _stratum = StratumConnector()
    return _stratum


__all__ = ["StratumConnector", "get_stratum", "wire_master_tools"]
