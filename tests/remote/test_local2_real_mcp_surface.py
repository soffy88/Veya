"""Real MCP surface qualification for Local2 (spec Phase 3).

These tests speak MCP to the running gateway over HTTP with the official SDK
client. Nothing here imports the adapter, the registry, or a tool function
directly, because the failure this phase exists to catch is a *wiring* failure:
a surface that looks right in-process and is unreachable, or reports the wrong
checkout, over the wire.

The whole module skips when the gateway is not listening, so an ordinary unit
run is not coupled to a live service. A skipped phase-3 gate is not a pass;
run it against a live gateway.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

MCP_HOST = "127.0.0.1"
MCP_PORT = 8790
MCP_URL = f"http://{MCP_HOST}:{MCP_PORT}/mcp"
SERVICE = "veya-remote-mcp.service"
CANONICAL_REPO = Path("/data/soffy/projects/veya")
AUTH_HEADER = Path.home() / ".veya" / "remote_mcp_auth_header"


def _gateway_listening() -> bool:
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((MCP_HOST, MCP_PORT)) == 0


requires_gateway = pytest.mark.skipif(
    not _gateway_listening(),
    reason="veya-remote-mcp.service is not listening; phase 3 must run against a live gateway",
)


def _auth() -> str:
    if not AUTH_HEADER.is_file():
        pytest.skip("no MCP bearer on disk; cannot authenticate")
    token = AUTH_HEADER.read_text(encoding="utf-8").strip()
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"


class _Session:
    """A real MCP client bound to a private event loop in a worker thread.

    The MCP SDK is anyio-based and opens task groups and cancel scopes during
    connect and teardown. pytest-asyncio runs this repo in auto mode, so a
    session opened on pytest's own loop tears down inside the wrong task. Giving
    the session its own loop in a worker thread keeps the SDK's scopes intact
    and makes the qualification independent of the surrounding plugin set.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def call(self, coro_factory: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro_factory(), self._loop).result(120)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=30)
        self._loop.close()


@pytest.fixture
def session():
    pytest.importorskip("mcp", reason="the mcp SDK is required to speak the real protocol")
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    state: dict[str, Any] = {}

    async def _open() -> None:
        client_cm = streamable_http_client(
            MCP_URL,
            http_client=httpx.AsyncClient(
                headers={"Authorization": _auth()},
                timeout=30.0,
            ),
        )
        read, write, _ = await client_cm.__aenter__()
        session_cm = ClientSession(read, write)
        client = await session_cm.__aenter__()
        state["init_result"] = await client.initialize()
        state["read"] = read
        state["write"] = write
        state["client_cm"] = client_cm
        state["session_cm"] = session_cm
        state["client"] = client

    holder = _Session(None)
    holder._loop = asyncio.new_event_loop()
    holder._thread = threading.Thread(target=holder._loop.run_forever, daemon=True)
    holder._thread.start()
    asyncio.run_coroutine_threadsafe(_open(), holder._loop).result(120)

    class _Proxy:
        def initialize(self):
            return state["init_result"]

        def list_tools(self):
            return holder.call(state["client"].list_tools)

        def call_tool(self, name, args):
            return holder.call(lambda: state["client"].call_tool(name, args))

    try:
        yield _Proxy()
    finally:

        async def _close() -> None:
            await state["session_cm"].__aexit__(None, None, None)
            await state["client_cm"].__aexit__(None, None, None)

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_close(), holder._loop).result(60)
        holder._loop.call_soon_threadsafe(holder._loop.stop)
        holder._thread.join(timeout=30)
        holder._loop.close()


# ── P3.1 service surface ────────────────────────────────────────────────


@requires_gateway
def test_gateway_is_a_real_service_not_a_fixture() -> None:
    """The surface must be a running service, not an in-process test double."""

    active = subprocess.run(
        ["systemctl", "--user", "is-active", SERVICE],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert active.stdout.strip() == "active", active.stdout
    pid = subprocess.run(
        ["systemctl", "--user", "show", SERVICE, "-p", "MainPID", "--value"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    assert pid.isdigit() and int(pid) > 0, pid
    # the listener belongs to that pid
    listeners = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=30).stdout
    assert f":{MCP_PORT} " in listeners, "gateway port not listening"
    assert f"pid={pid}" in listeners, "listener is not the service process"


# ── P3.2 initialize ─────────────────────────────────────────────────────


@requires_gateway
def test_initialize_over_the_real_protocol(session) -> None:
    result = session.initialize()
    assert result.protocolVersion
    assert result.serverInfo.name
    assert "tools" in result.capabilities.model_dump()


# ── P3.3 tools/list comes from the registry ─────────────────────────────


@requires_gateway
def test_tool_list_is_the_live_registry(session) -> None:
    from veya.remote.tool_adapter import BINDINGS

    listed = sorted(t.name for t in session.list_tools().tools)
    assert listed, "no tools advertised"
    # the advertised surface is the binding table, not a literal fixture
    assert set(listed) == {b.name for b in BINDINGS}
    assert not [name for name in listed if "hicode" in name.lower()], "retired executor advertised"


# ── P3.4 tools/call reaches real execution ──────────────────────────────


@requires_gateway
def test_shell_exec_runs_in_the_canonical_target(session) -> None:
    res = session.call_tool(
        "shell.exec",
        {
            "command": "pwd; echo SEP; git rev-parse --show-toplevel",
            "path": str(CANONICAL_REPO),
            "wait": True,
            "timeout_s": 60,
        },
    )
    payload = res.structuredContent or {}
    assert payload.get("ok") is True, payload
    inner = payload.get("result") or {}
    assert inner.get("cwd") == str(CANONICAL_REPO)
    assert inner.get("exit_code") == 0


@requires_gateway
def test_file_read_sees_untracked_canonical_source(session) -> None:
    probe = CANONICAL_REPO / "server" / "failure_semantics.py"
    if not probe.is_file():
        pytest.skip("canonical probe source is absent")
    res = session.call_tool("file.read", {"path": str(probe)})
    payload = res.structuredContent or {}
    assert payload.get("ok") is True, payload
    inner = payload.get("result") or {}
    evidence = (inner.get("resolution") or {}).get("evidence") or {}
    assert evidence.get("target_within_workspace") is True
    assert inner.get("text")


@requires_gateway
def test_default_mutation_never_touches_the_canonical_tree(session) -> None:
    probe = CANONICAL_REPO / ".mcp_phase3_mutation_probe.txt"
    assert not probe.exists(), "probe left over from a previous run"
    res = session.call_tool(
        "file.write", {"path": str(probe), "content": "PROBE\n", "overwrite": True}
    )
    payload = res.structuredContent or {}
    assert payload.get("ok") is True, payload
    # the owner's working tree must be exactly as clean of this probe as it was
    assert not probe.exists(), "default mutation polluted the canonical checkout"


@requires_gateway
def test_canonical_target_is_reachable_when_asked(session) -> None:
    probe = CANONICAL_REPO / ".mcp_phase3_explicit_canonical.txt"
    try:
        res = session.call_tool(
            "file.write",
            {
                "path": str(probe),
                "content": "EXPLICIT\n",
                "overwrite": True,
                "execution_target": "CANONICAL_WORKTREE",
            },
        )
        payload = res.structuredContent or {}
        assert payload.get("ok") is True, payload
        # explicit canonical is the caller's authority and must be honoured
        assert probe.is_file(), "explicit CANONICAL_WORKTREE did not reach the canonical tree"
    finally:
        if probe.exists():
            probe.unlink()


# ── P3.5 failure paths are refusals, not crashes ────────────────────────


@requires_gateway
def test_unknown_tool_is_refused_in_band(session) -> None:
    res = session.call_tool("definitely.not.a.tool", {})
    payload = res.structuredContent or {}
    assert res.isError is True
    assert payload.get("ok") is not True
    assert payload.get("error_code")


@requires_gateway
def test_out_of_workspace_write_is_refused(session) -> None:
    res = session.call_tool(
        "file.write", {"path": "/etc/mcp_phase3_probe.txt", "content": "x\n", "overwrite": True}
    )
    payload = res.structuredContent or {}
    assert payload.get("ok") is not True
    assert payload.get("error_code")
    assert not Path("/etc/mcp_phase3_probe.txt").exists()


@requires_gateway
def test_privileged_command_requires_approval(session) -> None:
    res = session.call_tool(
        "shell.exec",
        {
            "command": "sudo -n true",
            "path": str(CANONICAL_REPO),
            "wait": True,
            "timeout_s": 30,
        },
    )
    payload = res.structuredContent or {}
    assert payload.get("ok") is not True
    assert payload.get("error_code") in {"APPROVAL_REQUIRED", "POLICY_BLOCKED"}


# ── P3.6 the L0 receipt survives the wire ───────────────────────────────


@requires_gateway
def test_receipt_reaches_the_client(session) -> None:
    res = session.call_tool(
        "shell.exec",
        {"command": "pwd", "path": str(CANONICAL_REPO), "wait": True, "timeout_s": 60},
    )
    payload = res.structuredContent or {}
    execution_id = payload.get("execution_id")
    assert execution_id, payload

    status = session.call_tool("process.status", {"execution_id": execution_id})
    record = (status.structuredContent or {}).get("result") or {}
    assert record, status.structuredContent
    for field in (
        "execution_id",
        "status",
        "target_type",
        "dirty_state",
        "requested_workspace",
        "resolved_repo_root",
    ):
        assert field in record, f"receipt lost {field} on the way out"
    assert record["target_type"] == "CANONICAL_WORKTREE"
    assert record["status"] in {"COMPLETED", "FAILED", "TIMED_OUT"}
