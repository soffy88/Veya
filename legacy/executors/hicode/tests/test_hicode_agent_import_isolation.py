"""Hicode host/managed Python import-boundary regressions."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

from server import hicode_agent


@pytest.mark.asyncio
async def test_hicode_host_path_does_not_import_owner_3o(tmp_path: Path, monkeypatch) -> None:
    before = {
        name for name in ("omodul", "oprim", "obase", "oskill", "oservi") if name in sys.modules
    }
    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(hicode_agent, "_resolve_bin", lambda: "/managed/reasonix")
    monkeypatch.setattr(hicode_agent, "_snapshot_workspace", lambda ws, task: None)

    async def no_provider_run(*args, **kwargs):
        return {
            "subtype": "managed_bootstrap",
            "is_error": False,
            "result": "ready",
            "num_turns": 0,
        }

    monkeypatch.setattr(hicode_agent, "_run_hicode", no_provider_run)
    result = await hicode_agent._execute_hicode_core(
        "bootstrap-only test", workspace=str(tmp_path), force_cli=True
    )
    after = {
        name for name in ("omodul", "oprim", "obase", "oskill", "oservi") if name in sys.modules
    }

    assert "ready" in result
    assert after == before
    source = inspect.getsource(hicode_agent)
    assert "from veya.platform import load" not in source
    assert 'load("omodul")' not in source
    assert 'load("oprim")' not in source
