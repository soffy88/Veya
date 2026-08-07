"""server/open_design.py — Open Design MCP 接入装配层 (设计/渲染智能)。

3O 铁律: 机制 (stdio MCP 客户端, 含 jsonl 模式) 在主库 obase.mcp_stdio;
本模块只装配:
  - StdioMcpClient spawn `od mcp` (宿主 daemon 经网关 7456, token 认证) →
    McpClientRegistry.register("open_design");
  - 工具批量适配 → mcp_od_* 进 master_tools (主脑设计/渲染面);
  - 优雅降级: daemon/二进制不可达时跳过。

OD (宿主导航, /home/soffy/opendesign) 是 veya 的设计智能层 — 分镜/品牌合约/
原型/PPTX/MP4 渲染, 与 hevi (视频生成) / stratum (知识) / codebase (代码) 互补。

注意: 容器内 `od` 被 /usr/bin/od 抢占 — 必须用全路径 ~/.local/bin/od。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from veya.platform import obase as _load_obase

_obase = _load_obase()

OD_BIN = os.environ.get("OD_BIN", str(Path.home() / ".local" / "bin" / "od"))


def _od_token() -> str:
    """运行时读 env (测试 env 注入晚于模块 import)。"""
    return os.environ.get("OD_API_TOKEN", "")


def _od_daemon_url() -> str:
    # 桥 17456 → 宿主 loopback 7456 (od mcp 不带认证头, daemon 需 loopback 免 token)
    return os.environ.get("OD_DAEMON_URL", "http://192.168.16.1:17456")


class OpenDesignConnector:
    """Open Design MCP 装配器 (单例: app lifespan 启动)。"""

    def __init__(self, name: str = "open_design") -> None:
        self.name = name
        self._client: Any = None

    @property
    def available(self) -> bool:
        return Path(OD_BIN).is_file() and bool(_od_token())

    @property
    def ready(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "alive", False))

    async def start(self) -> None:
        """spawn `od mcp` (jsonl 协议) + 握手 + 注册。失败降级。"""
        if self.ready:
            return
        if not self.available:
            return
        StdioMcpClient = _obase.StdioMcpClient
        McpClientRegistry = _obase.McpClientRegistry

        try:
            # od mcp 代理 daemon 的认证 env 是 OD_TOOL_TOKEN (非 OD_API_TOKEN)
            env = {**os.environ, "OD_API_TOKEN": _od_token(),
                   "OD_TOOL_TOKEN": _od_token(),
                   "OD_DAEMON_URL": _od_daemon_url()}
            # jsonl 协议 (od mcp 非 LSP 帧); 全路径 (容器内 od 被 /usr/bin 抢占)
            client = StdioMcpClient([OD_BIN, "mcp"], env=env,
                                    name=self.name, startup_timeout=20.0,
                                    line_delimited=True)
            await client.start()
        except Exception:
            return      # daemon/二进制不可达 → 降级
        self._client = client
        McpClientRegistry.register(self.name, client)

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    def health(self) -> dict[str, Any]:
        if not self._client:
            return {"available": self.available, "reason": "未连接"}
        return {"available": True, **self._client.health()}

    # ── 设计/渲染查询 ──────────────────────────────────────────────────

    async def list_projects(self) -> list[dict[str, Any]]:
        res = await self._client.call_tool("list_projects", {})
        return res if isinstance(res, list) else []

    async def get_active_context(self) -> dict[str, Any] | None:
        res = await self._client.call_tool("get_active_context", {})
        return res if isinstance(res, dict) else None

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
                "name": f"mcp_od_{spec['name']}",
                "description": adapter.description,
                "parameters": {"type": "object", "properties": params, "required": required},
                "func": adapter.callable,
            })
        return out


async def wire_master_tools(connector: OpenDesignConnector | None = None) -> int:
    """mcp_od_* 注册进 master_tools (主脑设计/渲染面), 幂等。"""
    from server.tool_registry import master_tools

    connector = connector or get_open_design()
    added = 0
    for a in await connector.tool_adapters():
        if master_tools.has(a["name"]):
            continue
        master_tools.register(a["name"], a["description"], a["parameters"], a["func"],
                              max_result_chars=16000)
        added += 1
    return added


_od: OpenDesignConnector | None = None


def get_open_design() -> OpenDesignConnector:
    global _od
    if _od is None:
        _od = OpenDesignConnector()
    return _od


__all__ = ["OpenDesignConnector", "get_open_design", "wire_master_tools"]
