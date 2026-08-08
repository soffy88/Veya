"""server/stratum_memory.py — stratum 知识库 MCP 接入装配层。

3O 铁律: 机制 (HTTP MCP 客户端) 在主库 obase.mcp_http; 本模块只装配:
  - StreamableHttpMcpClient 连接 stratum-api /mcp/mcp (FastMCP 1.27 streamable);
  - 工具批量适配 → mcp_stratum_* 进 master_tools (主脑知识面);
  - 优雅降级: stratum 不可达/未装 fastmcp 时跳过。

stratum (AI 知识管家, 同宿主机) 是 veya 的知识智能层 — PDF/网页/RSS 入库、
三层融合检索 (BM25+向量)、翻译/摘要/朗读、概念图谱、记忆/会话上下文。
与 hevi (视频) / codebase (代码) 互补 — 主脑按问题类型路由:
  视频/动画 → mcp_hevi_*; 知识/检索/笔记 → mcp_stratum_*; 代码/文件 → codebase。
"""

from __future__ import annotations

import os
from typing import Any

from veya.platform import obase as _load_obase

_obase = _load_obase()

# FastMCP streamable_http_app 内部端点 /mcp, 经 http_api mount /mcp 后为 /mcp/mcp。
# 宿主 9309 端口已改为 0.0.0.0 绑定 (原 127.0.0.1 拒绝 veya 容器经宿网关访问)。
STRATUM_MCP_ENDPOINT = os.environ.get(
    "STRATUM_MCP_ENDPOINT", "http://192.168.16.1:9309/mcp/mcp")


class StratumConnector:
    """stratum MCP 装配器 (单例: app lifespan 启动)。"""

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
        StreamableHttpMcpClient = _obase.StreamableHttpMcpClient
        try:
            # stratum 的 mcp 服务端无 JWT (单租户 STRATUM_MCP_USER_ID 服务端绑定);
            # Host/Origin 头照 hevi 模式伪造 loopback (FastMCP transport 校验)。
            client = StreamableHttpMcpClient(
                self.endpoint, name=self.name, timeout=60.0,
                headers={
                    "Host": "127.0.0.1:9302",
                    "Origin": "http://127.0.0.1:9302",
                },
            )
            await client.start()
        except Exception:
            return      # stratum 不可达 → 降级
        self._client = client
        _obase.McpClientRegistry.register(self.name, client)

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    def health(self) -> dict[str, Any]:
        if not self._client:
            return {"available": False, "reason": "未连接"}
        return {"available": True, **self._client.health()}

    # ── 知识查询 (供 coordinator/路由直接调用) ─────────────────────────

    async def search_knowledge(self, query_text: str | None = None, top_k: int = 10,
                               *, query: str | None = None) -> Any:
        """融合检索 (BM25+向量) 主脑知识库。query 为旧版参数名兼容。

        MCP 服务端返回解析后的结果 (list[dict] 或 dict), 原样透传。
        """
        q = query_text or query or ""
        return await self._client.call_tool(
            "search_knowledge", {"query_text": q, "top_k": top_k})

    async def get_note(self, note_id: str) -> Any:
        """按 id 取笔记内容。"""
        return await self._client.call_tool("get_note", {"note_id": note_id})

    async def list_recent_notes(self, limit: int = 20) -> Any:
        """最近笔记列表。"""
        return await self._client.call_tool("list_recent_notes", {"limit": limit})

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
                "name": f"mcp_stratum_{spec['name'].replace('.', '_')}",
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
