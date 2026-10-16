import sys

from server.goal_run.routine_dispatch import run_probe


def test_command_probe_denies_by_default_even_with_execute_args(tmp_path):
    result = run_probe(
        {"kind": "command", "execute_args": [sys.executable, "-c", "print('unsafe')"]},
        str(tmp_path),
    )
    assert result["success"] is False
    assert "approval" in result["detail"]


def test_command_probe_rejects_shell_string_even_after_approval(tmp_path):
    result = run_probe(
        {"kind": "command", "command": "echo unsafe", "execute_args": ["echo", "unsafe"]},
        str(tmp_path),
        owner_authorized=True,
        approved=True,
    )
    assert result["success"] is False
    assert "execute_args" in result["detail"]


def test_command_probe_executes_argv_only_when_authorized_and_workspace_bound(tmp_path):
    result = run_probe(
        {"kind": "command", "execute_args": [sys.executable, "-c", "print('approved')"]},
        str(tmp_path),
        owner_authorized=True,
        approved=True,
    )
    assert result["success"] is True
    assert result["data"]["stdout"] == "approved"
    assert result["data"]["cwd"] == str(tmp_path.resolve())


def test_command_probe_rejects_workspace_escape_and_unbound_workspace(tmp_path):
    escape = run_probe(
        {"kind": "command", "execute_args": [sys.executable, "-c", "print('no')"], "cwd": ".."},
        str(tmp_path),
        owner_authorized=True,
        approved=True,
    )
    unbound = run_probe(
        {"kind": "command", "execute_args": [sys.executable, "-c", "print('no')"]},
        None,
        owner_authorized=True,
        approved=True,
    )
    assert escape["success"] is False
    assert "inside" in escape["detail"]
    assert unbound["success"] is False
    assert "workspace" in unbound["detail"]
