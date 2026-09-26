from __future__ import annotations

import time
from pathlib import Path

from runtime.coding.command_runner import CommandRunner, _safe_environment, redact_text
from runtime.coding.sandbox_profiles import get_sandbox_profile
from veya.remote.action_gateway import ActionCategory, classify_action
from veya.remote.models import RemotePermissions, RemoteSession


def _session(root: Path) -> RemoteSession:
    return RemoteSession(
        session_id="s",
        token_id="t",
        principal="p",
        permissions=RemotePermissions(read=True, write=True, shell=True, git=True),
        active_workspace=str(root),
        workspaces=[str(root)],
        created_at=time.time(),
        expires_at=time.time() + 3600,
    )


def test_workspace_full_is_the_default_executor_profile(tmp_path: Path) -> None:
    assert CommandRunner(tmp_path).profile.id == "l0_workspace_full"
    assert get_sandbox_profile("l0_workspace_full").network == "allowed"
    assert get_sandbox_profile("l0_isolated").network == "denied"


def test_shell_wrapper_recursively_allows_normal_development(tmp_path: Path) -> None:
    classification = classify_action(
        "shell.exec",
        {"command": "bash -lc 'pytest && ruff check .'"},
        _session(tmp_path),
        str(tmp_path),
    )
    assert classification.category == ActionCategory.AUTO_OPEN


def test_shell_wrapper_cannot_hide_privileged_operation(tmp_path: Path) -> None:
    classification = classify_action(
        "shell.exec",
        {"command": "bash -lc 'pytest && sudo apt-get install curl'"},
        _session(tmp_path),
        str(tmp_path),
    )
    assert classification.category == ActionCategory.REQUIRE_APPROVAL


def test_host_root_and_secret_discovery_are_denied(tmp_path: Path) -> None:
    session = _session(tmp_path)
    assert (
        classify_action("shell.exec", {"command": "rm -rf /"}, session, str(tmp_path)).category
        == ActionCategory.DENY
    )
    assert (
        classify_action(
            "shell.exec", {"command": "cat ~/.ssh/id_rsa"}, session, str(tmp_path)
        ).category
        == ActionCategory.DENY
    )


def test_workspace_delete_is_auto_open(tmp_path: Path) -> None:
    classification = classify_action(
        "shell.exec", {"command": "unlink file.txt"}, _session(tmp_path), str(tmp_path)
    )
    assert classification.category == ActionCategory.AUTO_OPEN


def test_provider_environment_is_explicit_and_redacted(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-inherit")
    env = _safe_environment(None)
    assert env["OPENAI_API_KEY"] == "provider-secret"
    assert "UNRELATED_SECRET" not in env
    assert redact_text("key=provider-secret", secret_values=[env["OPENAI_API_KEY"]]) == (
        "key=[REDACTED]"
    )
