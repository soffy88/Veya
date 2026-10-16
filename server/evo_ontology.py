"""server/evo_ontology.py — EvoOntology MCP preset & semantic tools integration (VEYA_EVO_ONTOLOGY).

Provides:
- EvoOntology stdio MCP preset definition (for McpClientRegistry / McpAttachPolicy).
- Real StdioMcpClient spawn against an ontology workspace (configurable path).
- Semantic tools `browse_semantics` & `resolve_semantics` mapped into Veya master_tools.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from server.feature_flags import enabled as _flag_enabled
from server.tool_registry import SideEffect, master_tools
from veya.platform import load

logger = logging.getLogger("veya.evo_ontology")

DEFAULT_VENDOR_MCP = "/data/soffy/vendor/EvoOntology/evoontology/runtime/mcp_server.py"

_client: Any = None
_client_ws: str | None = None


def is_evo_ontology_enabled() -> bool:
    try:
        return _flag_enabled("VEYA_EVO_ONTOLOGY")
    except KeyError:
        return False


def get_default_workspace() -> str:
    ws = os.environ.get("EVO_ONTOLOGY_WORKSPACE") or os.environ.get("EVOONTOLOGY_WORKSPACE")
    if ws:
        return str(Path(ws).resolve())
    return str((Path.cwd() / ".evoontology").resolve())


def get_evo_ontology_preset(workspace: str | Path | None = None) -> dict[str, Any]:
    """Return the EvoOntology MCP preset mapping."""
    ws = str(workspace or get_default_workspace())
    vendor_mcp = os.environ.get("EVOONTOLOGY_MCP_SERVER", DEFAULT_VENDOR_MCP)
    return {
        "name": "evoontology",
        "description": "EvoOntology — Self-Evolving Ontology Layer for Data Agents (browse_semantics / resolve_semantics)",
        "transport": "stdio",
        "runtime": "python",
        "command": [
            sys.executable,
            vendor_mcp,
            "--store",
            ws,
        ],
        "env": [],
        "side_effect": "external_read",
        "license": "MIT",
        "attribution": "https://github.com/ruc-datalab/EvoOntology",
        "line_delimited": True,
    }


async def get_evo_mcp_client(workspace: str | Path | None = None) -> Any:
    """Return or launch the StdioMcpClient for the given EvoOntology workspace."""
    global _client, _client_ws
    ws = str(workspace or get_default_workspace())

    if _client is not None and getattr(_client, "alive", False) and _client_ws == ws:
        return _client

    if _client is not None:
        try:
            await _client.close()
        except Exception:
            pass
        _client = None

    preset = get_evo_ontology_preset(ws)
    obase = load("obase")
    client = obase.StdioMcpClient(
        preset["command"],
        name="evoontology",
        line_delimited=True,
        startup_timeout=15.0,
        request_timeout=30.0,
    )
    await client.start()
    _client = client
    _client_ws = ws
    obase.McpClientRegistry.register("evoontology", client)
    return client


async def close_evo_mcp_client() -> None:
    """Close the active EvoOntology MCP client process."""
    global _client, _client_ws
    if _client is not None:
        try:
            await _client.close()
        except Exception:
            pass
        _client = None
        _client_ws = None


def _parse_mcp_tool_result(res: Any) -> dict[str, Any]:
    if isinstance(res, dict):
        content = res.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and "text" in first:
                try:
                    return json.loads(first["text"])
                except Exception:
                    return {"status": "ok", "text": first["text"]}
        return res
    return {"status": "ok", "result": res}


async def browse_semantics(
    query: str,
    workspace: str = "",
    version: str = "",
    kind: str = "all",
    limit: int = 6,
) -> dict[str, Any]:
    """Discover semantic concepts relevant to an analytical need."""
    if not is_evo_ontology_enabled():
        return {"status": "error", "message": "VEYA_EVO_ONTOLOGY is disabled"}
    target_ws = workspace or get_default_workspace()
    client = await get_evo_mcp_client(target_ws)
    args: dict[str, Any] = {"query": query}
    if workspace:
        args["workspace"] = workspace
    if version:
        args["version"] = version
    if kind:
        args["kind"] = kind
    if limit:
        args["limit"] = limit
    res = await client.call_tool("browse_semantics", args)
    return _parse_mcp_tool_result(res)


async def resolve_semantics(
    mentions: list[str],
    workspace: str = "",
    version: str = "",
    context: str = "",
) -> dict[str, Any]:
    """Resolve selected concepts to grounded mappings and linked objects."""
    if not is_evo_ontology_enabled():
        return {"status": "error", "message": "VEYA_EVO_ONTOLOGY is disabled"}
    target_ws = workspace or get_default_workspace()
    client = await get_evo_mcp_client(target_ws)
    args: dict[str, Any] = {"mentions": mentions}
    if workspace:
        args["workspace"] = workspace
    if version:
        args["version"] = version
    if context:
        args["context"] = context
    res = await client.call_tool("resolve_semantics", args)
    return _parse_mcp_tool_result(res)


async def wire_evo_ontology_tools(workspace: str | Path | None = None) -> int:
    """Wire browse_semantics and resolve_semantics into master_tools."""
    if not is_evo_ontology_enabled():
        return 0

    registered = 0
    if not master_tools.has("browse_semantics"):
        master_tools.register(
            name="browse_semantics",
            description="Discover semantic concepts in the ontology layer relevant to an analytical need. (EvoOntology MCP)",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query for concepts/terms"},
                    "workspace": {"type": "string", "description": "Optional ontology workspace directory"},
                    "version": {"type": "string", "description": "Optional semantic version; omit for active"},
                    "kind": {"type": "string", "enum": ["term", "mapping", "relation", "constraint", "all"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 6},
                },
                "required": ["query"],
            },
            func=browse_semantics,
            side_effect=SideEffect.PURE_READ,
        )
        registered += 1

    if not master_tools.has("resolve_semantics"):
        master_tools.register(
            name="resolve_semantics",
            description="Resolve selected concepts to grounded database mappings, relations, and constraints. (EvoOntology MCP)",
            parameters={
                "type": "object",
                "properties": {
                    "mentions": {"type": "array", "items": {"type": "string"}, "description": "Concept names or term IDs to resolve"},
                    "workspace": {"type": "string", "description": "Optional ontology workspace directory"},
                    "version": {"type": "string", "description": "Optional semantic version; omit for active"},
                    "context": {"type": "string", "description": "Optional query context to disambiguate"},
                },
                "required": ["mentions"],
            },
            func=resolve_semantics,
            side_effect=SideEffect.PURE_READ,
        )
        registered += 1

    return registered
