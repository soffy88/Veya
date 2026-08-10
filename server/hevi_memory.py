"""server/hevi_memory.py — hevi 视频管线 MCP 接入装配层。

3O 铁律: 机制 (HTTP MCP 客户端) 在主库 obase.mcp_http; 本模块只装配:
  - StreamableHttpMcpClient 连接 hevi-api /mcp/mcp (JWT 认证 + loopback 伪造);
  - 工具批量适配 → mcp_hevi_* 进 master_tools (主脑视频生产面);
  - 优雅降级: hevi 不可达/密钥缺失时跳过。

hevi (同宿主机, 同 docker 网络) 是 veya 的视频智能层 — generate_longvideo /
gen_storyboard / canvas / creative 技能, 与 codebase (代码) / stratum (知识) 互补。
"""

from __future__ import annotations

import os
from typing import Any

from veya.platform import obase as _load_obase

_obase = _load_obase()

HEVI_MCP_ENDPOINT = os.environ.get(
    "HEVI_MCP_ENDPOINT", "http://hevi-cftunnel-hevi-api-1:8000/mcp/mcp"
)


def _hevi_secret() -> str:
    """运行时读 env (模块级常量会在测试 env 注入后固化)。"""
    return os.environ.get("HEVI_JWT_SECRET", "")


def _sign_hevi_jwt() -> str | None:
    """签发 hevi MCP JWT (与 hevi-api 同款 obase.auth.jwt_create)。

    sub = HEVI_MCP_USER_ID (hevi 真实用户 UUID, hevi 工具按用户隔离数据)。
    """
    secret = _hevi_secret()
    user_id = os.environ.get("HEVI_MCP_USER_ID", "")
    if not secret or not user_id:
        return None
    try:
        import importlib

        from veya.platform import obase as _load_obase

        _load_obase()  # 触发 sys.path 注入 (宿主测试环境)
        auth = importlib.import_module("obase.auth")
        return auth.jwt_create(
            payload={"sub": user_id},
            secret=secret,
            expires_in_minutes=60,
        )
    except Exception:
        return None


class HeviConnector:
    """hevi MCP 装配器 (单例: app lifespan 启动)。"""

    def __init__(self, endpoint: str | None = None, name: str = "hevi") -> None:
        self.endpoint = endpoint or HEVI_MCP_ENDPOINT
        self.name = name
        self._client: Any = None

    @property
    def ready(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "_started", False))

    async def start(self) -> None:
        """握手 + 注册。失败降级 (不抛)。"""
        if self.ready:
            return
        token = _sign_hevi_jwt()
        if not token:
            return  # 密钥缺失 → 降级
        McpClientRegistry = _obase.McpClientRegistry
        StreamableHttpMcpClient = _obase.StreamableHttpMcpClient

        try:
            # hevi 的 mcp 2.0 校验 Host/Origin (DNS rebinding 防护) —
            # 容器内访问需伪造 loopback 头; JWT 认证走 Bearer。
            client = StreamableHttpMcpClient(
                self.endpoint,
                name=self.name,
                timeout=60.0,
                headers={
                    "Host": "127.0.0.1:8000",
                    "Origin": "http://127.0.0.1:8000",
                    "Authorization": f"Bearer {token}",
                },
            )
            await client.start()
        except Exception:
            return  # hevi 不可达 → 降级
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

    # ── 视频生产查询 (供 coordinator/路由直接调用) ─────────────────────

    async def generate_longvideo(
        self,
        topic: str,
        *,
        duration: str = "short",
        video_provider: str | None = None,
        audio_provider: str | None = None,
        style: str = "cinematic",
        language: str = "zh",
        **extra: Any,
    ) -> dict[str, Any]:
        """创建长视频生产任务 (hevi 视频管线核心)。"""
        args: dict[str, Any] = {
            "topic": topic,
            "duration_archetype": duration,
            "video_provider": video_provider or "",
            "audio_provider": audio_provider or "",
            "style": style,
            "language": language,
            **extra,
        }
        res = await self._client.call_tool("hevi.generate_longvideo", args)
        return res if isinstance(res, dict) else {"raw": res}

    async def gen_storyboard(self, script_text: str, shots: int = 6) -> dict[str, Any]:
        """分镜网格提示词生成。"""
        res = await self._client.call_tool(
            "hevi.gen_storyboard", {"script_text": script_text, "shots": shots}
        )
        return res if isinstance(res, dict) else {"raw": res}

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
            out.append(
                {
                    "name": f"mcp_hevi_{spec['name'].replace('.', '_')}",
                    "description": adapter.description,
                    "parameters": {"type": "object", "properties": params, "required": required},
                    "func": adapter.callable,
                }
            )
        return out


async def wire_master_tools(connector: HeviConnector | None = None) -> int:
    """mcp_hevi_* 注册进 master_tools (主脑视频生产面), 幂等。"""
    from server.tool_registry import register_mcp_tools

    connector = connector or get_hevi()
    # ②-B: 收成 1 个网关 mcp_hevi(action, args) (VEYA_MCP_GATEWAY=0 回退逐工具)
    return register_mcp_tools("hevi", await connector.tool_adapters(), max_result_chars=16000)


_hevi: HeviConnector | None = None


def get_hevi() -> HeviConnector:
    global _hevi
    if _hevi is None:
        _hevi = HeviConnector()
    return _hevi


__all__ = ["HeviConnector", "get_hevi", "wire_master_tools"]
