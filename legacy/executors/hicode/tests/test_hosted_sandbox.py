"""Hosted multi-user sandbox: no process isolation; per-user hicode volume."""

from __future__ import annotations

import pytest

from server import auth, hicode_agent
from server.tool_registry import ToolExecutionError, master_tools


@pytest.mark.asyncio
async def test_hosted_run_in_sandbox_uses_opensandbox(monkeypatch, tmp_path):
    from omodul.sandbox_broker import reset_broker
    from oprim._opensandbox import LoopbackOpenSandboxDriver, set_opensandbox_driver
    from oprim._sandbox_env import reset_sandbox_runtime

    monkeypatch.setenv("VEYA_SANDBOX_PROFILE", "hosted")
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    reset_sandbox_runtime()
    reset_broker()
    set_opensandbox_driver(LoopbackOpenSandboxDriver())
    auth.set_user({"user_id": "alice", "username": "alice"})
    out = await master_tools.execute("run_in_sandbox", {"code": "print(9 * 9)"})
    assert "exit_code=0" in out
    assert "81" in out
    assert "isolation=opensandbox" in out


@pytest.mark.asyncio
async def test_hosted_run_in_sandbox_fails_without_driver(monkeypatch, tmp_path):
    from omodul.sandbox_broker import reset_broker
    from oprim._opensandbox import set_opensandbox_driver
    from oprim._sandbox_env import reset_sandbox_runtime

    monkeypatch.setenv("VEYA_SANDBOX_PROFILE", "hosted")
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    reset_sandbox_runtime()
    reset_broker()
    set_opensandbox_driver(None)
    auth.set_user({"user_id": "alice", "username": "alice"})
    with pytest.raises(ToolExecutionError, match="opensandbox"):
        await master_tools.execute("run_in_sandbox", {"code": "print(1)"})


def test_hosted_hicode_workspace_is_per_user(monkeypatch, tmp_path):
    monkeypatch.setenv("VEYA_SANDBOX_PROFILE", "hosted")
    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))
    auth.set_user({"user_id": "alice", "username": "alice"})
    alice = hicode_agent._workspace_root()
    auth.set_user({"user_id": "bob", "username": "bob"})
    bob = hicode_agent._workspace_root()
    assert alice == (tmp_path / "users" / "alice").resolve()
    assert bob == (tmp_path / "users" / "bob").resolve()
    assert alice != bob
