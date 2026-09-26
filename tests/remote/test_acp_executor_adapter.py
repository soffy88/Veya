"""Tests for ACP (Agent Client Protocol) Executor Adapter (P2)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.acp_client import ACPBackend, ACPError
from veya.remote.acp_adapter import resolve_acp_command, run_acp_executor
from veya.remote.execution_contract import probe_runtime_capability_manifest


def test_resolve_acp_command(monkeypatch: pytest.MonkeyPatch) -> None:
    # Custom command takes top priority
    assert resolve_acp_command(["my-agent", "--flag"]) == ["my-agent", "--flag"]

    # Environment variable
    monkeypatch.setenv("VEYA_ACP_COMMAND", "custom-acp --run")
    assert resolve_acp_command() == ["custom-acp", "--run"]

    # Fallback to None if candidates not in PATH
    monkeypatch.delenv("VEYA_ACP_COMMAND", raising=False)
    with patch("shutil.which", return_value=None):
        assert resolve_acp_command() is None


def test_probe_acp_manifest_unavailable() -> None:
    with (
        patch("veya.remote.acp_adapter.resolve_acp_command", return_value=None),
        patch("shutil.which", return_value=None),
    ):
        manifest = probe_runtime_capability_manifest("acp")
        assert manifest.executor_id == "acp"
        assert manifest.status == "UNAVAILABLE"
        assert "not installed or not executable" in manifest.status_reason
        assert manifest.supports_streaming is True
        assert manifest.supports_cancel is True
        assert manifest.supports_session_reuse is True


def test_probe_acp_manifest_ready(tmp_path: Path) -> None:
    mock_bin = tmp_path / "openhands"
    mock_bin.write_text("#!/bin/sh\necho OpenHands 0.12.0\n")
    mock_bin.chmod(0o755)

    with patch("veya.remote.acp_adapter.resolve_acp_command", return_value=[str(mock_bin)]):
        manifest = probe_runtime_capability_manifest("acp")
        assert manifest.executor_id == "acp"
        assert manifest.status == "READY"
        assert manifest.installed is True
        assert manifest.supports_streaming is True


@pytest.mark.asyncio
async def test_run_acp_executor_success() -> None:
    reporter = MagicMock()
    reporter._execution_id = "ex_test1"
    reporter.update_message = MagicMock()
    reporter.finish_command = MagicMock()

    mock_backend = MagicMock(spec=ACPBackend)
    mock_backend.start_session = AsyncMock(return_value="sess_123")
    mock_backend._event_log = [
        {"event": {"type": "activity", "message": "Reading file"}},
        {"event": {"type": "text", "content": "Done"}},
    ]
    mock_backend.run = AsyncMock(
        return_value={"ok": True, "output": "Successfully refactored code", "events": 2}
    )
    mock_backend.close = AsyncMock()

    with (
        patch("veya.remote.acp_adapter.resolve_acp_command", return_value=["mock-agent"]),
        patch("veya.remote.acp_adapter.ACPBackend", return_value=mock_backend),
    ):
        out = await run_acp_executor(
            reporter,
            prompt="Refactor login module",
            command=["mock-agent"],
        )

        assert out == "Successfully refactored code"
        mock_backend.start_session.assert_awaited_once()
        mock_backend.run.assert_awaited_once()
        mock_backend.close.assert_awaited_once()
        reporter.finish_command.assert_called_once_with(exit_code=0, status="completed")


@pytest.mark.asyncio
async def test_run_acp_executor_cancellation() -> None:
    reporter = MagicMock()
    reporter._execution_id = "ex_cancel"
    reporter.update_message = MagicMock()
    reporter.finish_command = MagicMock()

    mock_backend = MagicMock(spec=ACPBackend)
    mock_backend.start_session = AsyncMock(return_value="sess_123")
    mock_backend._event_log = []

    async def cancel_side_effect(*args, **kwargs):
        raise asyncio.CancelledError()

    mock_backend.run = AsyncMock(side_effect=cancel_side_effect)
    mock_backend.cancel = AsyncMock()
    mock_backend.close = AsyncMock()

    with (
        patch("veya.remote.acp_adapter.resolve_acp_command", return_value=["mock-agent"]),
        patch("veya.remote.acp_adapter.ACPBackend", return_value=mock_backend),
    ):
        with pytest.raises(asyncio.CancelledError):
            await run_acp_executor(reporter, prompt="Long task")

        mock_backend.cancel.assert_awaited_once()
        mock_backend.close.assert_awaited_once()
        reporter.finish_command.assert_called_once_with(exit_code=130, status="cancelled")


@pytest.mark.asyncio
async def test_run_acp_executor_failure() -> None:
    reporter = MagicMock()
    reporter._execution_id = "ex_fail"
    reporter.update_message = MagicMock()
    reporter.finish_command = MagicMock()

    mock_backend = MagicMock(spec=ACPBackend)
    mock_backend.start_session = AsyncMock(side_effect=ACPError("Connection refused"))
    mock_backend.close = AsyncMock()

    with (
        patch("veya.remote.acp_adapter.resolve_acp_command", return_value=["mock-agent"]),
        patch("veya.remote.acp_adapter.ACPBackend", return_value=mock_backend),
    ):
        with pytest.raises(ACPError, match="Connection refused"):
            await run_acp_executor(reporter, prompt="Do fail")

        reporter.finish_command.assert_called_once_with(exit_code=1, status="failed")
        mock_backend.close.assert_awaited_once()
