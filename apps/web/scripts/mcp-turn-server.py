#!/usr/bin/env python3
"""Reference MCP turn server: one ``veya_turn`` tool backed by a local agent CLI.

This is the other half of veya's MCP bridge. Run it on the machine where
an agent CLI (Claude Code, Codex, Grok) is already logged in, register
it in veya as a runner with ``kind="mcp"`` pointing at this server, and
bind a bot to that runner: the bot's turns arrive here as MCP
``tools/call`` requests and are answered by the CLI's own output.

The prompt travels as the LAST argument of the configured command, so a
command like ``claude -p`` becomes ``claude -p "<prompt>"``. The CLI
uses its own login; veya never sees it, and this server never receives
a credential — only the runner token, checked as a Bearer token against
``MCP_TURN_SECRET`` on every request.

Usage:
    MCP_TURN_SECRET=<runner secret> python3 mcp-turn-server.py \
        --command "claude -p" --port 8901 --cwd ~/work

Then register (owner only):
    POST /api/v1/runners {"runner_id": "laptop-cli", "name": "Laptop CLI",
        "base_url": "http://100.64.0.9:8901/mcp", "token_ref": "MCP_TURN_SECRET",
        "kind": "mcp"}

Logs carry lengths and timings only, never prompt or reply content.
Stdlib only. Stateless: no session is stored or required.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import shlex
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_VERSION = "2025-03-26"
TOOL_NAME = "veya_turn"
MAX_BODY_BYTES = 1_048_576

logger = logging.getLogger("mcp-turn-server")


class TurnServer:
    """Config + JSON-RPC dispatch. Importable so tests can run it in-process."""

    def __init__(
        self,
        *,
        command: list[str],
        cwd: str | None,
        timeout_s: float,
        secret: str,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.timeout_s = timeout_s
        self.secret = secret

    def check_auth(self, headers) -> bool:
        presented = headers.get("Authorization") or ""
        expected = f"Bearer {self.secret}"
        return hmac.compare_digest(presented, expected)

    def handle(self, payload: object) -> tuple[int, dict]:
        if not isinstance(payload, dict):
            return 200, self._error(None, -32600, "invalid request: expected a JSON object")
        method = payload.get("method")
        request_id = payload.get("id")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}

        if method == "initialize":
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "veya-turn", "version": "0.1.0"},
                },
            }
        if method == "notifications/initialized":
            return 202, {}
        if method == "tools/list":
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": [
                        {
                            "name": TOOL_NAME,
                            "description": (
                                "Run one veya turn on the local agent CLI "
                                "and return its reply text."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "bot_id": {"type": "string"},
                                    "text": {"type": "string"},
                                    "context": {"type": "string"},
                                    "trace_id": {"type": "string"},
                                    "workspace_id": {"type": ["string", "null"]},
                                },
                                "required": ["text"],
                            },
                        }
                    ]
                },
            }
        if method == "tools/call":
            return self._tools_call(request_id, params)
        if isinstance(method, str):
            return 200, self._error(request_id, -32601, f"method not found: {method}")
        return 200, self._error(request_id, -32600, "invalid request: no method")

    def _error(self, request_id, code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def _tools_call(self, request_id, params: dict) -> tuple[int, dict]:
        name = params.get("name")
        arguments = params.get("arguments")
        if name != TOOL_NAME or not isinstance(arguments, dict):
            return 200, self._error(request_id, -32602, "unknown tool or bad arguments")
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return 200, self._error(request_id, -32602, "text must be a non-empty string")
        context = arguments.get("context")
        prompt = text
        if isinstance(context, str) and context.strip():
            prompt = f"{text}\n\n--- group context ---\n{context}"
        started = time.time()
        try:
            proc = subprocess.run(
                [*self.command, prompt],
                cwd=self.cwd,
                timeout=self.timeout_s,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            logger.warning("turn timed out after %.1fs", self.timeout_s)
            return 200, self._error(request_id, -32603, "command timed out")
        except (OSError, ValueError) as exc:
            logger.warning("turn failed to start: %s", type(exc).__name__)
            return 200, self._error(request_id, -32603, "command failed to start")
        elapsed = time.time() - started
        logger.info(
            "turn exit=%s elapsed=%.1fs stdout=%d stderr=%d",
            proc.returncode,
            elapsed,
            len(proc.stdout or ""),
            len(proc.stderr or ""),
        )
        if proc.returncode != 0:
            return 200, self._error(
                request_id, -32603, f"command failed (exit {proc.returncode})"
            )
        reply = (proc.stdout or "").strip()
        if not reply:
            return 200, self._error(request_id, -32603, "command produced no output")
        return 200, {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": reply}],
                "structuredContent": {
                    "task_id": f"mcp-{uuid.uuid4().hex[:16]}",
                    "session_id": "mcp",
                    "status": "completed",
                },
                "isError": False,
            },
        }


def make_handler(server: TurnServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep stdout clean; logger owns output
            return None

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler name
            if self.path.endswith("/health"):
                self._send_json(200, {"status": "ok"})
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler name
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send_json(400, {"error": "bad content length"})
                return
            if not server.check_auth(self.headers):
                self._send_json(401, {"error": "unauthorized"})
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send_json(200, server._error(None, -32700, "parse error"))
                return
            status, response = server.handle(payload)
            if status == 202:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_json(status, response)

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reference MCP turn server for veya (veya_turn tool)."
    )
    parser.add_argument(
        "--command",
        required=True,
        help='Agent CLI argv, e.g. "claude -p". The turn prompt is appended.',
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8901)
    parser.add_argument("--cwd", default=None, help="Working directory for the CLI.")
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args(argv)

    secret = os.environ.get("MCP_TURN_SECRET") or ""
    if not secret:
        print("MCP_TURN_SECRET is required (the runner token).", file=sys.stderr)
        return 2
    command = shlex.split(args.command)
    if not command:
        print("--command must not be empty.", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    turn_server = TurnServer(
        command=command, cwd=args.cwd, timeout_s=args.timeout, secret=secret
    )
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(turn_server))
    print(
        f"mcp-turn-server on http://{args.host}:{args.port} command={command} "
        f"cwd={args.cwd or '(inherit)'}",
        file=sys.stderr,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
