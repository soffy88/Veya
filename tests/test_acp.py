"""ACP stdio contract: initialize/session/prompt/cancel/permission/usage."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from server import acp
from server.acp.client import ACPClient
from server.engine_runner import run_engine, stream_engine
from veya.obase.approvals import ApprovalStore

FAKE_AGENT = str(Path(__file__).with_name("fake_acp_agent.py"))


def _client(tmp_path: Path) -> ACPClient:
    return ACPClient(
        [sys.executable, FAKE_AGENT],
        approvals=ApprovalStore(str(tmp_path / "a.db")),
    )


@pytest.mark.asyncio
async def test_initialize_session_prompt_usage_and_cancel(tmp_path):
    client = _client(tmp_path)
    try:
        sid = await client.new_session()
        assert sid == "acp-session-1"
        result = await client.prompt("hello", session_id=sid)
        assert result.ok and result.text == "echo:hello"
        assert result.usage["inputTokens"] == 3
        assert await client.cancel(session_id=sid) is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_permission_routes_through_risk_and_approval(tmp_path):
    client = _client(tmp_path)
    try:
        await client.new_session()
        outbound = await client.request_permission("send_email", {"to": "x@example.com"})
        assert outbound.ok is False
        assert "approval" in outbound.error
        row = client.approvals.get_approval(outbound.pending_permission or "")
        assert row is not None and row["tool"] == "send_email"
        read_only = await client.request_permission("local_read")
        assert read_only.ok is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_diagnostics_on_stderr_do_not_break_stdout(tmp_path, capsys):
    client = ACPClient(
        [sys.executable, "-u", FAKE_AGENT], approvals=ApprovalStore(str(tmp_path / "a.db"))
    )
    try:
        assert (await client.prompt("probe")).text == "echo:probe"
    finally:
        await client.close()


def test_adapter_normalization_and_missing_binary(monkeypatch, tmp_path):
    assert acp.normalize_adapter("claude_code") == "claude-code"
    assert acp.normalize_adapter("goose") == "goose"
    monkeypatch.setenv("VEYA_ACP", "0")
    assert acp.available_adapters() == ()
    monkeypatch.setenv("VEYA_ACP", "1")
    monkeypatch.setenv("VEYA_ACP_CODEX_CMD", str(tmp_path / "definitely-missing-acp"))
    assert "codex" not in acp.available_adapters()


@pytest.mark.asyncio
async def test_engine_runner_disabled_by_default_and_fake_adapter(monkeypatch, tmp_path):
    monkeypatch.delenv("VEYA_ACP", raising=False)
    assert (await run_engine("acp", "hi")).get("ok") is False
    monkeypatch.setenv("VEYA_ACP", "1")
    monkeypatch.setenv("VEYA_ACP_ADAPTER", "codex")
    monkeypatch.setenv("VEYA_ACP_CODEX_CMD", f"{sys.executable} {FAKE_AGENT}")
    result = await run_engine("acp", "hello", cwd=str(tmp_path))
    assert result.get("ok") is True and result.get("text") == "echo:hello"
    deltas = [event async for event in stream_engine("acp", "hello", cwd=str(tmp_path))]
    assert deltas[0]["type"] == "text_delta"
    assert deltas[-1]["type"] == "engine_done"


def test_capability_record_registered():
    from server.cli_capabilities import get_capability

    record = get_capability("acp")
    assert record.supports_resume is True
    assert record.output_format == "json"
