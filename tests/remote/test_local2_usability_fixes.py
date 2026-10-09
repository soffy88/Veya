"""LOCAL2-U1..U5 regression tests (ChatGPT-as-Codex usability fixes)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.coding.command_runner import CommandPolicyError, parse_command
from veya.remote.permission_engine import unmodelled_command_construct
from veya.remote.tool_adapter import RemoteToolAdapter, _link_canonical_venvs
from veya.remote.workspace_policy import classify_destructive


# U1: one persistent worktree per principal, not per MCP session
def test_task_id_is_stable_across_sessions_of_one_principal(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    adapter = RemoteToolAdapter.__new__(RemoteToolAdapter)
    a = SimpleNamespace(session_id="rs_a", principal="chatgpt-web")
    b = SimpleNamespace(session_id="rs_b", principal="chatgpt-web")
    other = SimpleNamespace(session_id="rs_c", principal="someone-else")
    ws = str(tmp_path)
    assert adapter._task_id(a, ws) == adapter._task_id(b, ws)
    assert adapter._task_id(a, ws) != adapter._task_id(other, ws)
    assert adapter._task_id(a, ws, "lane1") != adapter._task_id(a, ws)


# U3: bash -c / sh -lc is reduced, not refused; other interpreter forms are
@pytest.mark.parametrize(
    "command",
    ["bash -c 'echo hi && pwd'", "sh -c 'pytest -q'", "bash -lc 'git status | head'"],
)
def test_shell_c_wrapper_is_unwrapped(command: str) -> None:
    argv = parse_command(command)
    assert argv[:2] == ["/bin/bash", "-lc"]


@pytest.mark.parametrize("command", ["bash", "zsh -c 'echo'", "bash script.sh arg"])
def test_other_interpreter_forms_still_refused(command: str) -> None:
    with pytest.raises(CommandPolicyError):
        parse_command(command)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('python3 -c "import a; print(a)"', None),
        ("echo '$(literal)'", None),
        ("echo ok; rm -rf x", "semicolon_separator"),
        ('echo "$(id)"', "command_substitution"),
        ("echo $(id)", "command_substitution"),
        ("ls\nrm x", "newline_separator"),
        ("echo 'unbalanced", "unbalanced_quoting"),
    ],
)
def test_lexical_scan_only_counts_shell_constructs(command: str, expected: str | None) -> None:
    assert unmodelled_command_construct(command) == expected


# U4: project-local installs are development, everything else stays gated
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (".venv/bin/pip install requests", None),
        ("./venv/bin/python -m pip install -e .", None),
        ("uv pip install requests", None),
        ("uv pip install --system requests", "pip-install"),
        ("pip install requests", "pip-install"),
        ("../.venv/bin/pip install x", "pip-install"),
        (".venv/bin/pip install --target /usr x", "pip-install"),
        (".venv/bin/pip install x && rm -rf y", "rm"),
    ],
)
def test_project_local_install_classification(command: str, expected: str | None) -> None:
    assert classify_destructive(command) == expected


def test_canonical_venv_linked_only_when_ignored(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")
    wt.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert _link_canonical_venvs(wt, repo) == []  # not ignored -> never linked
    (repo / ".gitignore").write_text(".venv\n")
    assert _link_canonical_venvs(wt, repo) == [".venv"]
    assert (wt / ".venv").is_symlink()
    assert _link_canonical_venvs(wt, repo) == []  # never overwrites
