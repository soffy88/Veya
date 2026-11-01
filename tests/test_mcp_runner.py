"""MCP bridge: veya_turn tool calls, runner kinds, and the reference server."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from server.bot_team.conversations import ConversationStore
from server.bot_team.mcp_runner import (
    McpError,
    McpRunnerClient,
    McpTurnResult,
    forward_mcp_turn,
)
from server.bot_team.runner_client import TransportResponse
from server.bot_team.runners import RunnerError, RunnerRecord, RunnerRegistry
from server.goal_run.bot_identity import BotSpec

SECRET = "mcp-test-secret"
SECRET_REF = "VEYA_TEST_MCP_SECRET"
BASE_URL = "http://100.64.0.9:8901/mcp"
USER = {"user_id": "mcp-user", "username": "mcp"}


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_RUNNERS_PATH", str(tmp_path / "runners.json"))
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    monkeypatch.setenv(SECRET_REF, SECRET)
    return {"root": tmp_path}


class FakeMcpTransport:
    """Scripted MCP responses, keyed by method; records every call."""

    def __init__(self, *, call_result: dict | None = None, fail_first_init: bool = False):
        self.call_result = call_result or {
            "content": [{"type": "text", "text": "cli says hi"}],
            "structuredContent": {
                "task_id": "task-1",
                "session_id": "sess-1",
                "status": "completed",
            },
            "isError": False,
        }
        self.fail_first_init = fail_first_init
        self.calls: list[dict[str, Any]] = []
        self._inits = 0

    async def post(self, url, *, payload, timeout, headers):
        self.calls.append(
            {"url": url, "payload": payload, "timeout": timeout, "headers": headers}
        )
        method = payload.get("method")
        if method == "initialize":
            self._inits += 1
            if self.fail_first_init and self._inits == 1:
                return TransportResponse(status_code=0, error="Connection refused")
            return TransportResponse(
                status_code=200,
                body={
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "serverInfo": {"name": "fake", "version": "0"},
                    },
                },
            )
        if method == "notifications/initialized":
            return TransportResponse(status_code=202, body={})
        if method == "tools/call":
            return TransportResponse(
                status_code=200,
                body={
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": self.call_result,
                },
            )
        return TransportResponse(status_code=200, body={"jsonrpc": "2.0", "id": 1})


def _record() -> RunnerRecord:
    return RunnerRecord(
        runner_id="mcp1",
        name="Laptop CLI",
        base_url=BASE_URL,
        token_ref=SECRET_REF,
        kind="mcp",
    )


async def _call(transport, **overrides):
    client = McpRunnerClient(transport=transport, timeout=5.0)
    return await client.call_turn(
        _record(),
        bot_id="cli-bot",
        text="do the thing",
        context="",
        trace_id="tr-1",
        token=SECRET,
        **overrides,
    )


@pytest.mark.asyncio
async def test_mcp_call_turn_json():
    transport = FakeMcpTransport()
    result = await _call(transport)
    assert result == McpTurnResult(
        reply="cli says hi", task_id="task-1", session_id="sess-1", status="completed"
    )
    assert [c["payload"]["method"] for c in transport.calls] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert all(
        c["headers"]["Authorization"] == f"Bearer {SECRET}" for c in transport.calls
    )
    call_args = transport.calls[2]["payload"]["params"]["arguments"]
    assert call_args["text"] == "do the thing"
    assert call_args["bot_id"] == "cli-bot"
    assert call_args["trace_id"] == "tr-1"


@pytest.mark.asyncio
async def test_mcp_call_turn_sse_body():
    sse_body = (
        "event: message\ndata: "
        + json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"content": [{"type": "text", "text": "sse reply"}]},
            }
        )
        + "\n\n"
    )

    class SseTransport(FakeMcpTransport):
        async def post(self, url, *, payload, timeout, headers):
            self.calls.append(
                {"url": url, "payload": payload, "timeout": timeout, "headers": headers}
            )
            if payload.get("method") == "tools/call":
                return TransportResponse(status_code=200, body=sse_body)
            return await super().post(url, payload=payload, timeout=timeout, headers=headers)

    result = await _call(SseTransport())
    assert result.reply == "sse reply"


@pytest.mark.asyncio
async def test_mcp_tool_error_is_a_failed_turn():
    transport = FakeMcpTransport(
        call_result={
            "content": [{"type": "text", "text": "the CLI crashed"}],
            "isError": True,
        }
    )
    with pytest.raises(McpError, match="the CLI crashed"):
        await _call(transport)


@pytest.mark.asyncio
async def test_mcp_retry_on_transport_failure():
    transport = FakeMcpTransport(fail_first_init=True)
    result = await _call(transport)
    assert result.reply == "cli says hi"
    assert sum(1 for c in transport.calls if c["payload"]["method"] == "initialize") == 2


@pytest.mark.asyncio
async def test_mcp_unreadable_response_fails():
    class GarbageTransport(FakeMcpTransport):
        async def post(self, url, *, payload, timeout, headers):
            return TransportResponse(status_code=200, body="not json at all {{")

    with pytest.raises(McpError):
        await _call(GarbageTransport())


def test_runner_kind_round_trips(mcp_env):
    record = RunnerRegistry().add(
        runner_id="mcp1",
        name="Laptop CLI",
        base_url=BASE_URL,
        token_ref=SECRET_REF,
        kind="mcp",
    )
    assert record.kind == "mcp"
    assert record.to_view()["kind"] == "mcp"
    reloaded = RunnerRegistry().get("mcp1")
    assert reloaded is not None and reloaded.kind == "mcp"
    defaulted = RunnerRegistry().add(
        runner_id="mac",
        name="Mac",
        base_url="http://100.64.0.5:8765",
        token_ref=SECRET_REF,
    )
    assert defaulted.kind == "veya"
    with pytest.raises(RunnerError, match="unknown runner kind"):
        RunnerRegistry().add(
            runner_id="bogus",
            name="Bogus",
            base_url=BASE_URL,
            token_ref=SECRET_REF,
            kind="bogus",
        )


@pytest.mark.asyncio
async def test_forward_bot_turn_mcp(mcp_env):
    del mcp_env
    RunnerRegistry().add(
        runner_id="mcp1",
        name="Laptop CLI",
        base_url=BASE_URL,
        token_ref=SECRET_REF,
        kind="mcp",
    )
    bot = BotSpec(
        bot_id="cli-bot",
        owner_id=USER["user_id"],
        name="Cli Bot",
        runner_id="mcp1",
    )
    store = ConversationStore()
    from server.bot_team.runner_client import forward_bot_turn

    result = await forward_bot_turn(
        bot=bot,
        text="do the thing",
        user=dict(USER),
        conversations=store,
        mcp_client=McpRunnerClient(transport=FakeMcpTransport(), timeout=5.0),
    )
    assert result.reply == "cli says hi"
    assert result.task_id == "task-1"
    assert result.session_id == "sess-1"
    assert result.status == "completed"
    messages = store.list("cli-bot")
    assert [m["role"] for m in messages] == ["user", "bot"]
    assert messages[1]["text"] == "cli says hi"


def _load_turn_server():
    path = Path(__file__).resolve().parents[1] / "apps/web/scripts/mcp-turn-server.py"
    spec = importlib.util.spec_from_file_location("mcp_turn_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_reference_server_end_to_end():
    module = _load_turn_server()
    server = module.TurnServer(
        command=[sys.executable, "-c", "import sys; print('got:' + sys.argv[1])"],
        cwd=None,
        timeout_s=10.0,
        secret=SECRET,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), module.make_handler(server))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        record = RunnerRecord(
            runner_id="mcp1",
            name="Ref",
            base_url=f"http://127.0.0.1:{port}/mcp",
            token_ref=SECRET_REF,
            kind="mcp",
        )
        result = await McpRunnerClient(timeout=10.0).call_turn(
            record,
            bot_id="cli-bot",
            text="hello there",
            context="room context here",
            trace_id="tr-9",
            token=SECRET,
        )
        assert result.reply.startswith("got:hello there")
        assert "room context here" in result.reply
        assert result.task_id.startswith("mcp-")
        with pytest.raises(McpError):
            await McpRunnerClient(timeout=10.0).call_turn(
                record,
                bot_id="cli-bot",
                text="hello",
                token="wrong-secret",
            )
    finally:
        httpd.shutdown()
        httpd.server_close()

    # tools/list + health, over plain httpx, with the same server shape.
    import httpx

    server2 = module.TurnServer(
        command=[sys.executable, "-c", "print('x')"],
        cwd=None,
        timeout_s=10.0,
        secret=SECRET,
    )
    httpd2 = ThreadingHTTPServer(("127.0.0.1", 0), module.make_handler(server2))
    threading.Thread(target=httpd2.serve_forever, daemon=True).start()
    try:
        port2 = httpd2.server_address[1]
        headers = {"Authorization": f"Bearer {SECRET}"}
        listed = httpx.post(
            f"http://127.0.0.1:{port2}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=headers,
            timeout=5.0,
        )
        assert listed.status_code == 200
        assert listed.json()["result"]["tools"][0]["name"] == "veya_turn"
        health = httpx.get(f"http://127.0.0.1:{port2}/mcp/health", timeout=5.0)
        assert health.status_code == 200
        denied = httpx.post(
            f"http://127.0.0.1:{port2}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            timeout=5.0,
        )
        assert denied.status_code == 401
    finally:
        httpd2.shutdown()
        httpd2.server_close()
