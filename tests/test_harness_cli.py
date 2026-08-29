"""Tests for CLI `veya code` command.

Tests:
CLI-01 veya code objective
CLI-02 --json
CLI-03 --continue
CLI-04 --status
CLI-05 --diff
CLI-06 --artifacts
CLI-07 exit codes
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def test_repo(tmp_path: Path) -> Path:
    """Create a minimal test repository."""
    repo = tmp_path / "test_repo"
    repo.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True
    )

    # Create a simple file
    (repo / "main.py").write_text("def hello():\n    return 'hello'\n", encoding="utf-8")

    # Initial commit
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True
    )

    return repo


def test_cli_01_objective(test_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI-01: veya code with objective."""
    from cli.coding import run_coding_cli

    # Mock MasterAgent to return a result
    async def mock_chat_stream(*args, **kwargs):
        return {
            "final_answer": "Task completed",
            "tool_calls": [
                {
                    "tool": "coding_task_run",
                    "result": json.dumps(
                        {
                            "task_id": "ct_test123",
                            "status": "completed",
                            "acceptance_passed": True,
                            "changed_files": ["main.py"],
                            "final_summary": "Fixed the issue",
                        }
                    ),
                }
            ],
        }

    monkeypatch.setattr(
        "server.coordinator_master.master_coordinator.chat_stream", mock_chat_stream
    )

    # Run CLI
    result = run_coding_cli(["--path", str(test_repo), "Fix the bug"])

    # Should exit 0 for completed
    assert result == 0


def test_cli_02_json_output(
    test_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """CLI-02: veya code --json returns stable schema."""
    from cli.coding import run_coding_cli

    async def mock_chat_stream(*args, **kwargs):
        return {
            "final_answer": "Task completed",
            "tool_calls": [
                {
                    "tool": "coding_task_run",
                    "result": json.dumps(
                        {
                            "task_id": "ct_test123",
                            "status": "completed",
                            "acceptance_passed": True,
                            "changed_files": ["main.py"],
                            "final_summary": "Fixed the issue",
                            "artifact_ids": ["art_1"],
                            "verification_report_id": "vr_123",
                        }
                    ),
                }
            ],
        }

    monkeypatch.setattr(
        "server.coordinator_master.master_coordinator.chat_stream", mock_chat_stream
    )

    # Run CLI with --json
    result = run_coding_cli(["--path", str(test_repo), "--json", "Fix the bug"])

    # Should exit 0
    assert result == 0

    # Output should be valid JSON
    captured = capsys.readouterr()
    output = json.loads(captured.out)

    # Verify schema
    assert "task_id" in output
    assert "status" in output
    assert "acceptance_passed" in output


def test_cli_03_continue(test_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI-03: veya code --continue resumes task."""
    from runtime.coding.task_service import CodingTaskState, _write_task_state

    # Create an existing task
    state = CodingTaskState(
        task_id="ct_resume_test",
        workspace_path=str(test_repo),
        workspace_id="ws_test",
        objective="Resume this task",
        source="cli",
        status="partial_completed",
    )
    _write_task_state(test_repo, state)

    from cli.coding import run_coding_cli

    async def mock_chat_stream(*args, **kwargs):
        return {
            "final_answer": "Task resumed and completed",
            "tool_calls": [
                {
                    "tool": "coding_task_run",
                    "result": json.dumps(
                        {
                            "task_id": "ct_resume_test",
                            "status": "completed",
                            "acceptance_passed": True,
                        }
                    ),
                }
            ],
        }

    monkeypatch.setattr(
        "server.coordinator_master.master_coordinator.chat_stream", mock_chat_stream
    )

    # Run CLI with --continue
    result = run_coding_cli(["--path", str(test_repo), "--continue", "ct_resume_test"])

    # Should exit 0 for completed
    assert result == 0


def test_cli_04_status(test_repo: Path, capsys: pytest.CaptureFixture) -> None:
    """CLI-04: veya code --status shows task status."""
    from cli.coding import run_coding_cli
    from runtime.coding.task_service import CodingTaskState, _write_task_state

    # Create a task
    state = CodingTaskState(
        task_id="ct_status_test",
        workspace_path=str(test_repo),
        workspace_id="ws_test",
        objective="Check status",
        source="cli",
        status="completed",
    )
    _write_task_state(test_repo, state)

    # Run CLI with --path to specify the workspace
    result = run_coding_cli(["--path", str(test_repo), "--status", "ct_status_test"])

    # Should exit 0
    assert result == 0

    # Output should contain task info
    captured = capsys.readouterr()
    assert "ct_status_test" in captured.out
    assert "completed" in captured.out


def test_cli_05_diff(test_repo: Path, capsys: pytest.CaptureFixture) -> None:
    """CLI-05: veya code --diff shows task diff."""
    from cli.coding import run_coding_cli
    from runtime.coding.task_service import CodingTaskState, _write_task_state

    # Create a task with worktree
    state = CodingTaskState(
        task_id="ct_diff_test",
        workspace_path=str(test_repo),
        workspace_id="ws_test",
        objective="Show diff",
        source="cli",
        status="completed",
        worktree_path=str(test_repo),  # Use repo as worktree for test
    )
    _write_task_state(test_repo, state)

    # Run CLI
    result = run_coding_cli(["--diff", "ct_diff_test"])

    # Should exit 0 or 3 (no diff)
    assert result in (0, 3)


def test_cli_06_artifacts(test_repo: Path, capsys: pytest.CaptureFixture) -> None:
    """CLI-06: veya code --artifacts lists task artifacts."""
    from cli.coding import run_coding_cli
    from runtime.coding.task_service import CodingTaskState, _write_task_state

    # Create a task with artifacts
    state = CodingTaskState(
        task_id="ct_artifacts_test",
        workspace_path=str(test_repo),
        workspace_id="ws_test",
        objective="Show artifacts",
        source="cli",
        status="completed",
    )
    _write_task_state(test_repo, state)

    # Create an artifact
    outputs_dir = test_repo / ".veya" / "runs" / "ct_artifacts_test" / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    (outputs_dir / "test.txt").write_text("test", encoding="utf-8")

    # Run CLI
    result = run_coding_cli(["--artifacts", "ct_artifacts_test"])

    # Should exit 0
    assert result == 0


def test_cli_07_exit_codes(test_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI-07: Exit codes are correct."""
    from cli.coding import (
        EXIT_COMPLETED,
        EXIT_FAILED,
        EXIT_PARTIAL,
        run_coding_cli,
    )

    # Test completed
    async def mock_completed(*args, **kwargs):
        return {
            "tool_calls": [
                {
                    "tool": "coding_task_run",
                    "result": json.dumps(
                        {"task_id": "t1", "status": "completed", "acceptance_passed": True}
                    ),
                }
            ],
        }

    monkeypatch.setattr("server.coordinator_master.master_coordinator.chat_stream", mock_completed)
    assert run_coding_cli(["--path", str(test_repo), "test"]) == EXIT_COMPLETED

    # Test partial
    async def mock_partial(*args, **kwargs):
        return {
            "tool_calls": [
                {
                    "tool": "coding_task_run",
                    "result": json.dumps(
                        {"task_id": "t2", "status": "partial_completed", "acceptance_passed": False}
                    ),
                }
            ],
        }

    monkeypatch.setattr("server.coordinator_master.master_coordinator.chat_stream", mock_partial)
    assert run_coding_cli(["--path", str(test_repo), "test"]) == EXIT_PARTIAL

    # Test failed
    async def mock_failed(*args, **kwargs):
        return {
            "tool_calls": [
                {
                    "tool": "coding_task_run",
                    "result": json.dumps(
                        {"task_id": "t3", "status": "failed", "acceptance_passed": False}
                    ),
                }
            ],
        }

    monkeypatch.setattr("server.coordinator_master.master_coordinator.chat_stream", mock_failed)
    assert run_coding_cli(["--path", str(test_repo), "test"]) == EXIT_FAILED


def test_cli_invalid_workspace(tmp_path: Path) -> None:
    """CLI exits with INVALID_WORKSPACE for non-git directory."""
    from cli.coding import EXIT_INVALID_WORKSPACE, run_coding_cli

    non_git_dir = tmp_path / "not_a_repo"
    non_git_dir.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        run_coding_cli(["--path", str(non_git_dir), "test"])

    assert exc_info.value.code == EXIT_INVALID_WORKSPACE


def test_cli_no_objective(test_repo: Path) -> None:
    """CLI exits with error when no objective provided."""
    from cli.coding import run_coding_cli

    result = run_coding_cli(["--path", str(test_repo)])

    # Should exit with error
    assert result != 0


__all__ = [
    "test_cli_01_objective",
    "test_cli_02_json_output",
    "test_cli_03_continue",
    "test_cli_04_status",
    "test_cli_05_diff",
    "test_cli_06_artifacts",
    "test_cli_07_exit_codes",
    "test_cli_invalid_workspace",
    "test_cli_no_objective",
]
