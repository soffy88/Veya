"""AGY/OpenCode launch policy must remain below the V2 permission gateway."""

from __future__ import annotations

import time
from pathlib import Path

from veya.remote.action_gateway import ActionCategory, classify_action
from veya.remote.models import RemotePermissions, RemoteSession
from veya.remote.tool_adapter import _worker_command


def _session(tmp_path: Path) -> RemoteSession:
    now = time.time()
    return RemoteSession(
        session_id="agy-bypass-test",
        principal="test",
        token_id="token",
        workspaces=(str(tmp_path),),
        active_workspace=str(tmp_path),
        permissions=RemotePermissions(
            read=True,
            write=True,
            shell=True,
            git=True,
            network=True,
            service_control=True,
        ),
        created_at=now,
        expires_at=now + 60,
    )


def _agy_argv(tmp_path: Path, monkeypatch):
    binary = tmp_path / "agy"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("VEYA_ANTIGRAVITY_BIN", str(binary))
    monkeypatch.setenv("VEYA_ANTIGRAVITY_MODEL", "test-model")
    return _worker_command("antigravity", "write a file")


def test_agy_runtime_emits_expected_headless_bypass(tmp_path: Path, monkeypatch) -> None:
    argv, _ = _agy_argv(tmp_path, monkeypatch)
    assert "--dangerously-skip-permissions" in argv
    assert "--mode" in argv and "accept-edits" in argv


def test_opencode_runtime_never_sets_dangerous_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_DANGEROUSLY_SKIP_PERMISSIONS", "1")
    from veya.remote.tool_adapter import _opencode_runtime_env

    env = _opencode_runtime_env()
    assert "OPENCODE_DANGEROUSLY_SKIP_PERMISSIONS" not in env


def test_agy_normal_workspace_write_still_allowed(tmp_path: Path) -> None:
    classification = classify_action(
        "file.write", {"path": "probe.txt"}, _session(tmp_path), str(tmp_path)
    )
    assert classification.category == ActionCategory.AUTO_OPEN


def test_agy_git_commit_still_allowed(tmp_path: Path) -> None:
    classification = classify_action(
        "shell.exec",
        {"command": "git commit -m qualification"},
        _session(tmp_path),
        str(tmp_path),
    )
    assert classification.category == ActionCategory.AUTO_OPEN
    assert classification.capability_id == "git.normal"


def test_agy_privileged_action_requires_v2_approval(tmp_path: Path) -> None:
    classification = classify_action(
        "shell.exec", {"command": "sudo id"}, _session(tmp_path), str(tmp_path)
    )
    assert classification.category == ActionCategory.HUMAN_GATED
    assert classification.requires_approval is True


def test_agy_retry_preserves_expected_headless_bypass(tmp_path: Path, monkeypatch) -> None:
    first, _ = _agy_argv(tmp_path, monkeypatch)
    second, _ = _agy_argv(tmp_path, monkeypatch)
    assert "--dangerously-skip-permissions" in first
    assert "--dangerously-skip-permissions" in second
