"""server.goal_run.git_diff — 任务前后 diff 抓取, 供双轴代码审查用。"""

from __future__ import annotations

import subprocess

from server.goal_run.git_diff import capture_task_diff, current_head


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "a.txt").write_text("orig\n")
    subprocess.run(["git", "add", "a.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def test_current_head_non_git_dir_returns_empty(tmp_path):
    assert current_head(str(tmp_path)) == ""


def test_current_head_git_dir_returns_hash(tmp_path):
    _init_repo(tmp_path)
    head = current_head(str(tmp_path))
    assert len(head) == 40


def test_capture_task_diff_uncommitted_changes(tmp_path):
    _init_repo(tmp_path)
    before = current_head(str(tmp_path))
    (tmp_path / "a.txt").write_text("changed\n")
    diff = capture_task_diff(str(tmp_path), before)
    assert "changed" in diff
    assert "-orig" in diff or "orig" in diff


def test_capture_task_diff_committed_changes(tmp_path):
    _init_repo(tmp_path)
    before = current_head(str(tmp_path))
    (tmp_path / "b.txt").write_text("new file\n")
    subprocess.run(["git", "add", "b.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add b"], cwd=tmp_path, check=True)
    diff = capture_task_diff(str(tmp_path), before)
    assert "b.txt" in diff
    assert "new file" in diff


def test_capture_task_diff_no_changes_is_empty(tmp_path):
    _init_repo(tmp_path)
    before = current_head(str(tmp_path))
    diff = capture_task_diff(str(tmp_path), before)
    assert diff == ""


def test_capture_task_diff_non_git_dir_returns_empty(tmp_path):
    assert capture_task_diff(str(tmp_path), "somehash") == ""
