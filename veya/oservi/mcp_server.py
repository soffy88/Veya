"""
veya/mcp_server.py — MCP (Model Context Protocol) Server (Layer 4).

Implements a JSON-RPC 2.0 MCP server that exposes veya's 3O tools and skills
as MCP tools. Compatible with Claude Desktop, Cursor, and other MCP clients.

Protocol: https://spec.modelcontextprotocol.io/
Transport: stdio (default) + HTTP/SSE (optional)
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 types
# ---------------------------------------------------------------------------


@dataclass
class JSONRPCRequest:
    jsonrpc: str = "2.0"
    id: int | str | None = None
    method: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class JSONRPCResponse:
    jsonrpc: str = "2.0"
    id: int | str | None = None
    result: Any = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict:
        d: dict = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.error:
            d["error"] = self.error
        else:
            d["result"] = self.result
        return d


# ---------------------------------------------------------------------------
# MCP Tool registry
# ---------------------------------------------------------------------------


@dataclass
class MCPTool:
    """An MCP tool definition."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    handler: Callable | None = None


class MCPToolRegistry:
    """Registry of MCP tools exposed by this server."""

    def __init__(self):
        self._tools: dict[str, MCPTool] = {}

    def register(self, tool: MCPTool):
        self._tools[tool.name] = tool

    def list_tools(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    def get(self, name: str) -> MCPTool | None:
        return self._tools.get(name)

    def register_from_3o_elements(self):
        """Auto-register tools from 3O element aliases."""
        try:
            from veya.server.manifests import ELEMENT_ALIASES, resolve_element

            for spec_name in sorted(ELEMENT_ALIASES):
                element = resolve_element(spec_name)
                if element is not None:
                    self.register(
                        MCPTool(
                            name=spec_name.replace(".", "_"),
                            description=f"3O element: {spec_name}",
                            input_schema={
                                "type": "object",
                                "properties": {
                                    "arguments": {
                                        "type": "object",
                                        "description": f"Arguments for {spec_name}",
                                    },
                                },
                            },
                            handler=element,
                        )
                    )
        except Exception:
            pass

    def register_veya_tools(self):
        """Register veya's built-in tools."""
        # Browser tools
        self.register(
            MCPTool(
                name="browser_navigate",
                description="Navigate to a URL using Playwright browser",
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to navigate to"},
                        "headless": {"type": "boolean", "default": True},
                    },
                    "required": ["url"],
                },
            )
        )

        self.register(
            MCPTool(
                name="browser_screenshot",
                description="Take a screenshot of the current browser page",
                input_schema={
                    "type": "object",
                    "properties": {
                        "selector": {"type": "string", "description": "CSS selector (optional)"},
                    },
                },
            )
        )

        self.register(
            MCPTool(
                name="spawn_agent",
                description="Spawn an external AI coding agent (Claude Code, Codex, etc.)",
                input_schema={
                    "type": "object",
                    "properties": {
                        "agent_name": {
                            "type": "string",
                            "enum": ["claude-code", "codex", "aider", "cursor"],
                        },
                        "prompt": {"type": "string"},
                        "workdir": {"type": "string", "default": "."},
                        "timeout_sec": {"type": "number", "default": 300},
                    },
                    "required": ["agent_name", "prompt"],
                },
            )
        )

        # Voice tools
        self.register(
            MCPTool(
                name="speech_to_text",
                description="Transcribe speech audio to text",
                input_schema={
                    "type": "object",
                    "properties": {
                        "audio_base64": {"type": "string"},
                        "provider": {"type": "string", "default": "openai"},
                        "language": {"type": "string", "default": "en"},
                    },
                    "required": ["audio_base64"],
                },
            )
        )

        self.register(
            MCPTool(
                name="text_to_speech",
                description="Synthesize speech from text",
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "voice": {"type": "string"},
                        "provider": {"type": "string", "default": "openai"},
                    },
                    "required": ["text"],
                },
            )
        )

        # Vision tools
        self.register(
            MCPTool(
                name="analyze_image",
                description="Analyze an image using a vision-capable LLM",
                input_schema={
                    "type": "object",
                    "properties": {
                        "image_base64": {"type": "string"},
                        "prompt": {"type": "string", "default": "Describe this image."},
                        "provider": {"type": "string", "default": "openai"},
                    },
                    "required": ["image_base64"],
                },
            )
        )

        # Code tools
        self.register(
            MCPTool(
                name="code_review",
                description="Review code for bugs, security, and style issues",
                input_schema={
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "language": {"type": "string", "default": "python"},
                    },
                    "required": ["code"],
                },
            )
        )

        self.register(
            MCPTool(
                name="ripgrep_search",
                description="Fast code search using ripgrep",
                input_schema={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {"type": "string", "default": "."},
                        "glob": {"type": "string"},
                    },
                    "required": ["pattern"],
                },
            )
        )

        # Knowledge tools
        self.register(
            MCPTool(
                name="knowledge_search",
                description="Search the agent's knowledge store",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "type": {"type": "string", "default": "all"},
                    },
                    "required": ["query"],
                },
            )
        )


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------


class MCPServer:
    """JSON-RPC 2.0 MCP Server.

    Implements the MCP protocol over stdio or HTTP.

    MCP Methods:
        - initialize: Client handshake
        - tools/list: List available tools
        - tools/call: Call a specific tool
        - resources/list: List resources (optional)
        - prompts/list: List prompts (optional)

    Usage (stdio):
        server = MCPServer()
        server.run_stdio()

    Usage (HTTP):
        # Mount as FastAPI router:
        router = server.as_fastapi_router()
        app.include_router(router, prefix="/mcp")
    """

    def __init__(self, name: str = "veya-mcp", version: str = "0.1.0"):
        self.name = name
        self.version = version
        self.tools = MCPToolRegistry()
        self.tools.register_veya_tools()
        self.tools.register_from_3o_elements()
        self._initialized = False
        self._client_info: dict = {}

    # ── MCP Method handlers ────────────────────────────────────────────

    async def handle_initialize(self, params: dict) -> dict:
        """Handle MCP initialize handshake."""
        self._client_info = params.get("clientInfo", {})
        self._initialized = True
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {
                "name": self.name,
                "version": self.version,
            },
            "capabilities": {
                "tools": {},
                "resources": {},
            },
        }

    async def handle_tools_list(self, params: dict | None = None) -> dict:
        """Handle MCP tools/list."""
        return {"tools": self.tools.list_tools()}

    async def handle_tools_call(self, params: dict) -> dict:
        """Handle MCP tools/call."""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        tool = self.tools.get(tool_name)
        if tool is None:
            return {
                "content": [{"type": "text", "text": f"Tool not found: {tool_name}"}],
                "isError": True,
            }

        if tool.handler is None:
            # Try dynamic resolution
            try:
                from veya.server.manifests import resolve_element

                spec_name = tool_name.replace("_", ".")
                element = resolve_element(spec_name)
                if element is not None:
                    result = element(**arguments) if arguments else element()
                    if hasattr(result, "__await__"):
                        result = await result
                    return {
                        "content": [{"type": "text", "text": json.dumps(result, default=str)}],
                    }
            except Exception as e:
                return {
                    "content": [{"type": "text", "text": f"Tool execution failed: {e}"}],
                    "isError": True,
                }
            return {
                "content": [{"type": "text", "text": f"No handler for tool: {tool_name}"}],
                "isError": True,
            }

        try:
            result = tool.handler(**arguments) if arguments else tool.handler()
            if hasattr(result, "__await__"):
                result = await result
            return {
                "content": [{"type": "text", "text": str(result)}],
            }
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True,
            }

    async def handle_resources_list(self, params: dict | None = None) -> dict:
        """Handle MCP resources/list."""
        return {"resources": []}

    async def handle_prompts_list(self, params: dict | None = None) -> dict:
        """Handle MCP prompts/list."""
        return {"prompts": []}

    # ── JSON-RPC dispatch ──────────────────────────────────────────────

    async def handle_request(self, raw: str) -> str:
        """Handle a raw JSON-RPC request string. Returns response string."""
        try:
            req_data = json.loads(raw)
        except json.JSONDecodeError:
            return json.dumps(
                JSONRPCResponse(
                    id=None,
                    error={"code": -32700, "message": "Parse error"},
                ).to_dict()
            )

        req = JSONRPCRequest(
            id=req_data.get("id"),
            method=req_data.get("method", ""),
            params=req_data.get("params", {}),
        )

        # Route to method handler
        method_handlers = {
            "initialize": self.handle_initialize,
            "tools/list": self.handle_tools_list,
            "tools/call": self.handle_tools_call,
            "resources/list": self.handle_resources_list,
            "prompts/list": self.handle_prompts_list,
            "notifications/initialized": lambda p: {},
        }

        handler = method_handlers.get(req.method)
        if handler is None:
            return json.dumps(
                JSONRPCResponse(
                    id=req.id,
                    error={"code": -32601, "message": f"Method not found: {req.method}"},
                ).to_dict()
            )

        try:
            result = handler(req.params)
            if hasattr(result, "__await__"):
                result = await result
            return json.dumps(
                JSONRPCResponse(
                    id=req.id,
                    result=result,
                ).to_dict()
            )
        except Exception as e:
            return json.dumps(
                JSONRPCResponse(
                    id=req.id,
                    error={"code": -32603, "message": str(e)},
                ).to_dict()
            )

    # ── Transport ──────────────────────────────────────────────────────

    def run_stdio(self):
        """Run MCP server over stdio (standard MCP transport)."""
        loop = asyncio.get_event_loop()

        async def _stdio_loop():
            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)

            writer_transport, writer_protocol = await loop.connect_write_pipe(
                asyncio.streams.FlowControlMixin, sys.stdout
            )
            writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, loop)

            while True:
                try:
                    line = await reader.readline()
                    if not line:
                        break
                    raw = line.decode().strip()
                    if not raw:
                        continue
                    response = await self.handle_request(raw)
                    writer.write((response + "\n").encode())
                    await writer.drain()
                except Exception:
                    break

        loop.run_until_complete(_stdio_loop())

    def as_fastapi_router(self):
        """Return a FastAPI router for HTTP transport."""
        try:
            from fastapi import APIRouter, Request
            from fastapi.responses import JSONResponse
        except ImportError:
            raise RuntimeError("fastapi required for HTTP transport")

        router = APIRouter()

        @router.post("/jsonrpc")
        async def mcp_jsonrpc(request: Request):
            body = await request.body()
            response = await self.handle_request(body.decode())
            return JSONResponse(content=json.loads(response))

        @router.get("/health")
        async def mcp_health():
            return {
                "status": "ok",
                "server": self.name,
                "version": self.version,
                "tools_count": len(self.tools.list_tools()),
            }

        return router


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def create_mcp_server(name: str = "veya-mcp") -> MCPServer:
    """Create a configured MCP server."""
    return MCPServer(name=name)


if __name__ == "__main__":
    # Run as stdio MCP server
    server = create_mcp_server()
    server.run_stdio()
