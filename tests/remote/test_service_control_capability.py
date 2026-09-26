from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from veya.remote.models import RemotePermissions, RemoteSession
from veya.remote.tool_adapter import (
    RemoteToolAdapter,
    _allowed_service_control_command,
    _parse_service_control_command,
)


def _repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _session(path: Path, *, service_control: bool, destructive: bool = False) -> RemoteSession:
    now = time.time()
    return RemoteSession(
        session_id="rs-test",
        principal="chatgpt-web",
        token_id="rt-test",
        workspaces=(str(path.resolve()),),
        active_workspace=str(path.resolve()),
        permissions=RemotePermissions(
            read=True,
            write=True,
            shell=True,
            git=True,
            destructive=destructive,
            service_control=service_control,
        ),
        created_at=now,
        expires_at=now + 3600,
    )


class FakeProcess:
    def __init__(
        self,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def test_service_control_permission_roundtrip() -> None:
    permissions = RemotePermissions(
        read=True,
        write=True,
        shell=True,
        git=True,
        destructive=False,
        service_control=True,
    )
    restored = RemotePermissions.from_dict(permissions.to_dict())
    assert restored.service_control is True
    assert restored.destructive is False


# 1. test_service_control_daemon_reload_allowed
@pytest.mark.parametrize(
    "command",
    [
        "systemctl --user daemon-reload",
        "/usr/bin/systemctl --user daemon-reload",
        "/bin/systemctl --user daemon-reload",
    ],
)
async def test_service_control_daemon_reload_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    _repo(tmp_path)
    assert _allowed_service_control_command(command) is True
    assert _parse_service_control_command(command) == ("daemon-reload", "")

    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path, service_control=True)
    captured: list[tuple[str, ...]] = []

    async def fake_exec(*argv: str, **kwargs: Any) -> FakeProcess:
        captured.append(argv)
        return FakeProcess(0, b"", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": command,
            "approved": True,
            "workspace": str(tmp_path),
        },
    )
    assert result.ok is True
    assert result.result["service_control"] is True
    assert result.result["action"] == "daemon-reload"
    assert result.result["destructive_capability_used"] is False
    assert captured == [("/usr/bin/systemctl", "--user", "daemon-reload")]


# 2. test_service_control_daemon_reload_rejects_extra_args_or_system
@pytest.mark.parametrize(
    "command",
    [
        "systemctl daemon-reload",
        "systemctl --system daemon-reload",
        "/usr/bin/systemctl --system daemon-reload",
        "systemctl --user daemon-reload extra",
        "/usr/bin/systemctl --user daemon-reload anything",
        "systemctl --user daemon-reload --now",
        "systemctl --user daemon-reload; id",
    ],
)
async def test_service_control_daemon_reload_rejects_extra_args_or_system(
    tmp_path: Path, command: str
) -> None:
    _repo(tmp_path)
    assert _allowed_service_control_command(command) is False
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path, service_control=True, destructive=False)
    result = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": command,
            "approved": True,
            "workspace": str(tmp_path),
        },
    )
    assert result.ok is False
    assert str(result.error_code) in ("POLICY_BLOCKED", "INVALID_APPROVAL")


# 3. test_service_control_all_actions_allowed_for_managed_units
@pytest.mark.parametrize(
    "action",
    ["restart", "start", "stop", "is-active", "status"],
)
@pytest.mark.parametrize(
    "unit",
    ["veya-remote-mcp.service", "veya-openai-tunnel.service"],
)
def test_service_control_all_actions_allowed_for_managed_units(action: str, unit: str) -> None:
    for prefix in ("systemctl", "/usr/bin/systemctl", "/bin/systemctl"):
        cmd = f"{prefix} --user {action} {unit}"
        assert _allowed_service_control_command(cmd) is True
        assert _parse_service_control_command(cmd) == (action, unit)


# 4. test_service_control_rejects_unmanaged_units
@pytest.mark.parametrize(
    "command",
    [
        "systemctl --user restart ssh.service",
        "systemctl --user status nginx.service",
        "systemctl --user is-active any-other.service",
        "systemctl --user start systemd-journald.service",
        "systemctl --user restart veya-remote-mcp",
        "systemctl --user restart *",
        "systemctl --user restart veya-*.service",
        "systemctl --user status docker.service",
    ],
)
async def test_service_control_rejects_unmanaged_units(tmp_path: Path, command: str) -> None:
    _repo(tmp_path)
    assert _allowed_service_control_command(command) is False
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path, service_control=True, destructive=False)
    result = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": command,
            "approved": True,
            "workspace": str(tmp_path),
        },
    )
    assert result.ok is False
    assert str(result.error_code) in ("POLICY_BLOCKED", "INVALID_APPROVAL")


# 5. test_service_control_rejects_extra_flags
@pytest.mark.parametrize(
    "command",
    [
        "systemctl --user restart veya-remote-mcp.service --no-block",
        "systemctl --user --now restart veya-remote-mcp.service",
        "systemctl --user -f restart veya-remote-mcp.service",
        "systemctl --user restart veya-remote-mcp.service --force",
        "systemctl --user status -l veya-remote-mcp.service",
        "systemctl --user status veya-remote-mcp.service --lines=10",
        "systemctl --user is-active --quiet veya-remote-mcp.service",
    ],
)
async def test_service_control_rejects_extra_flags(tmp_path: Path, command: str) -> None:
    _repo(tmp_path)
    assert _allowed_service_control_command(command) is False
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path, service_control=True, destructive=False)
    result = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": command,
            "approved": True,
            "workspace": str(tmp_path),
        },
    )
    assert result.ok is False
    assert str(result.error_code) in ("POLICY_BLOCKED", "INVALID_APPROVAL")


# 6. test_service_control_rejects_shell_wrappers
@pytest.mark.parametrize(
    "command",
    [
        'bash -lc "systemctl --user restart veya-remote-mcp.service"',
        'sh -c "systemctl --user restart veya-remote-mcp.service"',
        "systemctl --user restart veya-remote-mcp.service; id",
        "systemctl --user restart veya-remote-mcp.service && echo 1",
        "systemctl --user restart veya-remote-mcp.service | cat",
        "systemctl --user restart $(echo veya-remote-mcp.service)",
        "sudo systemctl --user restart veya-remote-mcp.service",
        "sudo systemctl restart veya-remote-mcp.service",
        "sudo -u root systemctl --user restart veya-remote-mcp.service",
    ],
)
async def test_service_control_rejects_shell_wrappers(tmp_path: Path, command: str) -> None:
    _repo(tmp_path)
    assert _allowed_service_control_command(command) is False
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path, service_control=True, destructive=False)
    result = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": command,
            "approved": True,
            "workspace": str(tmp_path),
        },
    )
    assert result.ok is False
    assert str(result.error_code) in ("POLICY_BLOCKED", "INVALID_APPROVAL")


# 7. test_service_control_self_restart_transient
async def test_service_control_self_restart_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path, service_control=True)
    captured: list[tuple[str, ...]] = []

    async def fake_exec(*argv: str, **kwargs: Any) -> FakeProcess:
        captured.append(argv)
        return FakeProcess(0, b"", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": "systemctl --user restart veya-remote-mcp.service",
            "approved": True,
            "workspace": str(tmp_path),
        },
    )
    assert result.ok is True
    assert result.result["service_control"] is True
    assert result.result["action"] == "restart"
    assert result.result["unit"] == "veya-remote-mcp.service"
    assert result.result["scheduled"] is True
    assert result.result["delay_ms"] == 500
    assert result.result["accepted"] is True
    assert result.result["destructive_capability_used"] is False

    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == "/usr/bin/systemd-run"
    assert "--user" in argv
    assert "--quiet" in argv
    assert "--collect" in argv
    assert any(a.startswith("--unit=veya-service-restart-") for a in argv)
    assert "--on-active=500ms" in argv
    assert argv[-4:] == (
        "/usr/bin/systemctl",
        "--user",
        "restart",
        "veya-remote-mcp.service",
    )


# 8. test_service_control_self_stop_transient
async def test_service_control_self_stop_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path, service_control=True)
    captured: list[tuple[str, ...]] = []

    async def fake_exec(*argv: str, **kwargs: Any) -> FakeProcess:
        captured.append(argv)
        return FakeProcess(0, b"", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": "systemctl --user stop veya-remote-mcp.service",
            "approved": True,
            "workspace": str(tmp_path),
        },
    )
    assert result.ok is True
    assert result.result["service_control"] is True
    assert result.result["action"] == "stop"
    assert result.result["unit"] == "veya-remote-mcp.service"
    assert result.result["scheduled"] is True
    assert result.result["delay_ms"] == 500
    assert result.result["accepted"] is True
    assert result.result["destructive_capability_used"] is False

    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == "/usr/bin/systemd-run"
    assert "--user" in argv
    assert "--quiet" in argv
    assert "--collect" in argv
    assert any(a.startswith("--unit=veya-service-stop-") for a in argv)
    assert "--on-active=500ms" in argv
    assert argv[-4:] == (
        "/usr/bin/systemctl",
        "--user",
        "stop",
        "veya-remote-mcp.service",
    )


# 9. test_service_control_status_parsing_and_exit_code_3
async def test_service_control_status_parsing_and_exit_code_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path, service_control=True)

    # Case A: active state (rc=0)
    async def fake_status_active(*argv: str, **kwargs: Any) -> FakeProcess:
        stdout = (
            b"\xe2\x97\x8f veya-remote-mcp.service - Veya Remote MCP gateway\n"
            b"     Loaded: loaded (/home/soffy/.config/systemd/user/veya-remote-mcp.service; enabled)\n"
            b"     Active: active (running) since Sat 2026-09-26 00:30:56 UTC; 19min ago\n"
            b"   Main PID: 1501790\n"
        )
        return FakeProcess(0, stdout, b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_status_active)
    res_active = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": "systemctl --user status veya-remote-mcp.service",
            "approved": True,
            "workspace": str(tmp_path),
        },
    )
    assert res_active.ok is True
    assert res_active.result["service_control"] is True
    assert res_active.result["action"] == "status"
    assert res_active.result["unit"] == "veya-remote-mcp.service"
    assert res_active.result["state"] == "active"
    assert res_active.result["exit_code"] == 0
    assert "Active: active (running)" in res_active.result["text"]

    # Case B: inactive dead state (rc=3)
    async def fake_status_inactive(*argv: str, **kwargs: Any) -> FakeProcess:
        stdout = (
            b"\xe2\x97\x8b veya-remote-mcp.service - Veya Remote MCP gateway\n"
            b"     Loaded: loaded (/home/soffy/.config/systemd/user/veya-remote-mcp.service; enabled)\n"
            b"     Active: inactive (dead)\n"
        )
        return FakeProcess(3, stdout, b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_status_inactive)
    res_inactive = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": "systemctl --user status veya-remote-mcp.service",
            "approved": True,
            "workspace": str(tmp_path),
        },
    )
    assert res_inactive.ok is True
    assert res_inactive.result["service_control"] is True
    assert res_inactive.result["action"] == "status"
    assert res_inactive.result["unit"] == "veya-remote-mcp.service"
    assert res_inactive.result["state"] == "inactive"
    assert res_inactive.result["exit_code"] == 3

    # Case C: failed state (rc=3)
    async def fake_status_failed(*argv: str, **kwargs: Any) -> FakeProcess:
        stdout = (
            b"\xc3\x97 veya-remote-mcp.service - Veya Remote MCP gateway\n"
            b"     Loaded: loaded (/home/soffy/.config/systemd/user/veya-remote-mcp.service; enabled)\n"
            b"     Active: failed (Result: exit-code) since Sat 2026-09-26 00:30:56 UTC\n"
        )
        return FakeProcess(3, stdout, b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_status_failed)
    res_failed = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": "systemctl --user status veya-remote-mcp.service",
            "approved": True,
            "workspace": str(tmp_path),
        },
    )
    assert res_failed.ok is True
    assert res_failed.result["state"] == "failed"
    assert res_failed.result["exit_code"] == 3

    # Case D: text truncation to 4000 chars
    async def fake_status_long(*argv: str, **kwargs: Any) -> FakeProcess:
        stdout = b"Active: active\n" + (b"A" * 6000)
        return FakeProcess(0, stdout, b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_status_long)
    res_long = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": "systemctl --user status veya-remote-mcp.service",
            "approved": True,
            "workspace": str(tmp_path),
        },
    )
    assert res_long.ok is True
    assert len(res_long.result["text"]) == 4000


# 10. test_service_control_requires_service_control_permission_not_destructive
async def test_service_control_requires_service_control_permission_not_destructive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)

    async def must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("command execution must stay blocked")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", must_not_run)
    monkeypatch.setattr(adapter, "_call_command_tool", must_not_run)

    # 1. service_control=False, destructive=False, approved=True -> BLOCKED
    res1 = await adapter._call_impl(
        _session(tmp_path, service_control=False, destructive=False),
        "shell.exec",
        {
            "command": "systemctl --user restart veya-remote-mcp.service",
            "approved": True,
            "workspace": str(tmp_path),
        },
    )
    assert res1.ok is False
    assert str(res1.error_code) == "POLICY_BLOCKED"

    # 2. service_control=False, destructive=True, approved=True -> STILL BLOCKED!
    # (destructive=True must NOT bypass service_control requirement)
    res2 = await adapter._call_impl(
        _session(tmp_path, service_control=False, destructive=True),
        "shell.exec",
        {
            "command": "systemctl --user restart veya-remote-mcp.service",
            "approved": True,
            "workspace": str(tmp_path),
        },
    )
    assert res2.ok is False
    assert str(res2.error_code) == "POLICY_BLOCKED"

    # 3. service_control=True, destructive=False -> SUCCEEDS under V1.0 (approved field ignored for AUTO_OPEN)
    async def fake_exec(*argv: str, **kwargs: Any) -> FakeProcess:
        return FakeProcess(0, b"", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    res3 = await adapter._call_impl(
        _session(tmp_path, service_control=True, destructive=False),
        "shell.exec",
        {
            "command": "systemctl --user restart veya-remote-mcp.service",
            "approved": False,
            "workspace": str(tmp_path),
        },
    )
    assert res3.ok is True
    assert res3.result["service_control"] is True

    # 4. service_control=True, destructive=False, approved=True -> SUCCEEDS!
    res4 = await adapter._call_impl(
        _session(tmp_path, service_control=True, destructive=False),
        "shell.exec",
        {
            "command": "systemctl --user restart veya-remote-mcp.service",
            "approved": True,
            "workspace": str(tmp_path),
        },
    )
    assert res4.ok is True
    assert res4.result["service_control"] is True
    assert res4.result["destructive_capability_used"] is False
