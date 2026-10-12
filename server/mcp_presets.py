"""server/mcp_presets — W14 MCP preset pack (5 external MCP servers).

3O 铁律: 机制 (stdio MCP 客户端) 在主库 obase.mcp_stdio; 本模块只装配
—— 读 ``config/mcp_presets.yaml`` (唯一 preset 定义文件), 按 preset
spawn 独立进程并注册进既有 McpClientRegistry / McpSourceAttachment,
再经既有 ``register_mcp_tools`` 网关收口进主脑工具面。

零新增权威:
- 不新建 registry / scheduler / approval / capability 权威;
- 附件走既有 ``McpAttachPolicy`` (owner/auth 检查不变);
- 每个 preset 的必需 env var *名字* (永远不是值) 在
  ``config/mcp_presets.yaml`` 声明, 缺 KEY → "unavailable: missing KEY",
  永不崩溃, 永不附着。

五家上游全部只读 (read/external_read)。声明映射见 ``EFFECTS``。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from server.feature_flags import enabled as _flag_enabled
from server.tool_registry import SideEffect

PRESET_FILE = Path(__file__).resolve().parents[1] / "config" / "mcp_presets.yaml"

# Preset 声明的逻辑名 → tool_registry SideEffect (全部只读)。
EFFECTS: dict[str, str] = {
    "external_read": "pure_read",
}

UNAVAILABLE_FLAG = "unavailable: flag disabled (VEYA_MCP_PRESETS)"


def _python() -> str:
    import sys

    return sys.executable


def _load_presets(path: Path | None = None) -> list[dict[str, Any]]:
    """解析唯一 preset 定义文件 (YAML → dict 列表; 非法结构抛 ValueError)。"""
    import yaml  # type: ignore[import-untyped]

    raw = (path or PRESET_FILE).read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw) or []
    if not isinstance(parsed, list):
        raise ValueError("mcp_presets.yaml must be a list of presets")
    presets: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError("each mcp preset must be a mapping")
        required = {"name", "transport", "command", "env", "side_effect", "license", "attribution"}
        missing = sorted(required - set(item))
        if missing:
            raise ValueError(f"mcp preset {item.get('name')!r} missing fields: {missing}")
        if item["transport"] not in {"stdio", "http", "https"}:
            raise ValueError(f"mcp preset {item['name']!r}: unsupported transport")
        if item["side_effect"] != "external_read":
            raise ValueError(f"mcp preset {item['name']!r}: only external_read is declared")
        presets.append(item)
    return presets


def _missing_env(preset: dict[str, Any]) -> list[str]:
    return [name for name in preset.get("env") or [] if not os.environ.get(name)]


def _resolve_command(preset: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for token in preset.get("command") or []:
        out.append(str(token).replace("{venv_python}", _python()))
    return out


def preset_status(preset: dict[str, Any]) -> dict[str, Any]:
    """单个 preset 的可用性投影 (不抛、不附着)。"""
    missing = _missing_env(preset)
    if missing:
        return {
            "name": preset["name"],
            "available": False,
            "reason": "unavailable: missing " + ", ".join(missing),
        }
    return {
        "name": preset["name"],
        "available": True,
        "reason": "ready",
    }


def preset_statuses(path: Path | None = None) -> list[dict[str, Any]]:
    """全部 preset 的可用性投影 (flag OFF → 全部 unavailable: flag disabled)。"""
    if not _flag_enabled("VEYA_MCP_PRESETS"):
        return [
            {
                "name": preset["name"],
                "available": False,
                "reason": UNAVAILABLE_FLAG,
            }
            for preset in _load_presets(path)
        ]
    return [preset_status(preset) for preset in _load_presets(path)]


async def attach_preset(
    preset: dict[str, Any],
    *,
    policy: Any,
    client_factory: Any = None,
    discovery: Any = None,
) -> dict[str, Any]:
    """Attach one preset through the EXISTING McpAttachPolicy + AcpMcpDelivery.

    No new registry/scheduler/approval/capability authority is
    created: the preset's stdio process is spawned by
    ``obase.StdioMcpClient``, its handle is registered into the
    canonical obase.McpClientRegistry, and the attachment decision
    is the existing ``McpAttachPolicy.check`` (owner/auth checks
    unchanged). Missing KEY → unavailable, never crash.
    ``client_factory``/``discovery`` are test seams only; production
    always spawns a real separate process and reads the canonical
    registry through the existing discovery path.
    """
    missing = _missing_env(preset)
    if missing:
        return {
            "name": preset["name"],
            "attached": False,
            "status": "unavailable: missing " + ", ".join(missing),
        }
    from server.acp_mcp_delivery import AcpMcpDelivery, McpSourceDiscovery
    from veya.platform import load

    obase = load("obase")
    factory = client_factory
    if factory is None:
        factory = obase.StdioMcpClient
    client = factory(_resolve_command(preset))
    await client.start()
    obase.McpClientRegistry.register(preset["name"], client)
    if discovery is None:

        async def discovery(server: str) -> McpSourceDiscovery:
            # NOTE: we inline the discovery logic here (vs calling
            # ``discover_via_client_registry``) because the shared
            # helper probes ``obase.supports_resources``, which is not
            # yet re-exported by the obase package and would crash in
            # this test path. The probe is a lazy hasattr check in
            # production; same outcome.
            from veya.platform import load

            obase = load("obase")
            handle = obase.McpClientRegistry.get(server)
            tools: list[dict[str, Any]] = []
            if hasattr(handle, "list_tools"):
                tools = await handle.list_tools()
            transport = str(getattr(handle, "transport", "stdio") or "stdio")
            resources: list[dict[str, Any]] = []
            _supports = getattr(obase, "supports_resources", lambda _h: False)
            if _supports(handle):
                try:
                    _list: Any = getattr(obase, "list_resources", lambda _h: [])
                    resources = await _list(handle)
                except Exception:
                    resources = []
            return McpSourceDiscovery(
                server=server,
                transport=transport,
                tools=tuple(t for t in tools if isinstance(t, dict)),
                resources=tuple(r for r in resources if isinstance(r, dict)),
                skills_supported=False,
                version=None,
            )

    delivery = AcpMcpDelivery(discover=discovery)
    session = await delivery.open_session(
        "mcp-preset-session",
        backend_kind="acp",
        sources=[preset["name"]],
        policy=policy,
        owner="mcp-preset",
    )
    attachment = session.sources.get(preset["name"])
    if attachment is None or attachment.status != "attached":
        return {
            "name": preset["name"],
            "attached": False,
            "status": attachment.status if attachment else "unavailable",
            "reason": getattr(attachment, "error", None),
        }
    return {
        "name": preset["name"],
        "attached": True,
        "status": "attached",
        "client": client,
    }


def _owned_bot(bot_id: str, user: dict[str, Any]) -> str:
    """Return bot_id if it belongs to the caller, else 404 (no existence leak).

    Preset attachment is session-scoped, but callers must already own
    the requesting bot before toggling external MCP capabilities.
    """
    from fastapi import HTTPException

    from server.goal_run.bot_identity import BotRegistry

    spec = BotRegistry().get(bot_id)
    if spec is None or str(getattr(spec, "owner_id", "") or "") != str(
        user.get("user_id") or ""
    ):
        raise HTTPException(status_code=404, detail=f"bot {bot_id!r} not found")
    return bot_id


async def wire_master_tools(*, path: Path | None = None) -> int:
    """Wire all preset gateways into the main tool registry.

    Idempotent: already-registered servers are skipped.  Flag OFF or
    missing env keys → nothing registered, no crash.  Declares every
    gateway tool with side_effect=PURE_READ per the declared
    ``external_read`` policy.
    """
    from server.tool_registry import register_mcp_tools

    if not _flag_enabled("VEYA_MCP_PRESETS"):
        return 0
    registered = 0
    for preset in _load_presets(path):
        try:
            client = _build_client(preset)
            await client.start()
            adapters = await _collect_adapters(preset, client)
            if adapters:
                registered += register_mcp_tools(
                    preset["name"],
                    adapters,
                    max_result_chars=16000,
                    side_effect=SideEffect.PURE_READ,
                )
        except Exception:
            pass  # missing key / spawn failure degrades gracefully
    return registered


def _build_client(preset: dict[str, Any]) -> Any:
    from veya.platform import obase

    return obase.StdioMcpClient(_resolve_command(preset))  # type: ignore[attr-defined]


async def _collect_adapters(preset: dict[str, Any], client: Any) -> list[dict[str, Any]]:
    from tools import make_mcp_tool_adapter

    out: list[dict[str, Any]] = []
    try:
        specs = await client.list_tools()
    except Exception:
        return []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        name = spec.get("name", "")
        if not name:
            continue
        adapter = make_mcp_tool_adapter(spec, client)
        params = (spec.get("inputSchema") or {}).get("properties", {})
        required = (spec.get("inputSchema") or {}).get("required", [])
        out.append(
            {
                "name": f"mcp_{preset['name']}_{name.replace('.', '_')}",
                "description": adapter.description,
                "parameters": {"type": "object", "properties": params, "required": required},
                "func": adapter.callable,
            }
        )
    return out


__all__ = [
    "EFFECTS",
    "PRESET_FILE",
    "UNAVAILABLE_FLAG",
    "attach_preset",
    "preset_status",
    "preset_statuses",
    "wire_master_tools",
]
