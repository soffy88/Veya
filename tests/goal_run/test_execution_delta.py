from __future__ import annotations

import subprocess
from datetime import UTC, datetime

from server.goal_run.execution_delta import capture_git_state, execution_delta
from server.goal_run.models import GoalRunState


def _git(root, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def test_execution_delta_separates_preexisting_and_execution_paths(tmp_path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Veya Test")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "initial")

    tracked.write_text("preexisting dirty\n", encoding="utf-8")
    preexisting = tmp_path / "preexisting.txt"
    preexisting.write_text("keep\n", encoding="utf-8")
    before = capture_git_state(str(tmp_path))

    created = tmp_path / "created.txt"
    created.write_text("execution\n", encoding="utf-8")
    tracked.write_text("execution edit\n", encoding="utf-8")
    preexisting.unlink()
    after = capture_git_state(str(tmp_path))

    delta = execution_delta(before, after)
    assert delta["preexisting_dirty"] == ["preexisting.txt", "tracked.txt"]
    assert delta["execution_created"] == ["created.txt"]
    assert delta["execution_modified"] == ["tracked.txt"]
    assert delta["execution_deleted"] == ["preexisting.txt"]


def test_goal_run_accept_done_projection_round_trips() -> None:
    state = GoalRunState(goal_id="goal-1", goal_text="test")
    state.acceptance_verdict = "ACCEPT"
    state.done_at = datetime.now(UTC)
    restored = GoalRunState.from_taskgraph_json(state.to_taskgraph_json(), state.goal_text)
    assert restored.acceptance_verdict == "ACCEPT"
    assert restored.done_at == state.done_at
