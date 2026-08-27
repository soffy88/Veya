"""goal_run × 双轴代码审查接线证明: 真实 diff 触发 review, 空 diff 不触发。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from server.goal_run.models import TaskNode
from server.goal_run.runner import _run_dual_axis_review


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "a.txt").write_text("orig\n")
    subprocess.run(["git", "add", "a.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


@pytest.mark.asyncio
async def test_dual_axis_review_runs_on_real_diff(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    from server.goal_run.git_diff import current_head

    before = current_head(str(tmp_path))
    (tmp_path / "a.txt").write_text("changed by task\n")

    called_with = {}

    async def fake_dual_axis_review(**kwargs):
        called_with.update(kwargs)
        return {
            "standards": {"findings": [], "worst": None},
            "spec": {"findings": [], "worst": "看起来漏了个 edge case"},
        }

    monkeypatch.setattr("server.goal_run.code_review.dual_axis_review", fake_dual_axis_review)

    task = TaskNode(
        id="t1",
        title="改 a.txt",
        instruction="把 a.txt 改成 changed",
        acceptance=["文件内容变了"],
        depends_on=[],
        assignee="hicode",
    )
    result = await _run_dual_axis_review(task, str(tmp_path), before)

    assert result is not None
    assert result["spec"]["worst"] == "看起来漏了个 edge case"
    assert "changed by task" in called_with["diff_text"]
    assert called_with["task_instruction"] == "把 a.txt 改成 changed"


@pytest.mark.asyncio
async def test_dual_axis_review_skips_on_empty_diff(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    from server.goal_run.git_diff import current_head

    before = current_head(str(tmp_path))
    calls = []
    monkeypatch.setattr(
        "server.goal_run.code_review.dual_axis_review",
        lambda **kw: calls.append(1),
    )

    task = TaskNode(
        id="t1", title="x", instruction="x", acceptance=[], depends_on=[], assignee="hicode"
    )
    result = await _run_dual_axis_review(task, str(tmp_path), before)

    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_dual_axis_review_reads_claude_md_as_standards_doc(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("Never use global state.\n")
    from server.goal_run.git_diff import current_head

    before = current_head(str(tmp_path))
    (tmp_path / "a.txt").write_text("changed\n")

    captured = {}

    async def fake_dual_axis_review(**kwargs):
        captured.update(kwargs)
        return {
            "standards": {"findings": [], "worst": None},
            "spec": {"findings": [], "worst": None},
        }

    monkeypatch.setattr("server.goal_run.code_review.dual_axis_review", fake_dual_axis_review)

    task = TaskNode(
        id="t1", title="x", instruction="x", acceptance=[], depends_on=[], assignee="hicode"
    )
    await _run_dual_axis_review(task, str(tmp_path), before)

    assert "Never use global state." in captured["standards_doc"]


@pytest.mark.asyncio
async def test_dual_axis_review_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_GOAL_RUN_CODE_REVIEW_ENABLED", "0")
    task = TaskNode(
        id="t1", title="x", instruction="x", acceptance=[], depends_on=[], assignee="hicode"
    )
    result = await _run_dual_axis_review(task, str(tmp_path), "")
    assert result is None


@pytest.mark.asyncio
async def test_dual_axis_review_exception_returns_none_not_crash(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    from server.goal_run.git_diff import current_head

    before = current_head(str(tmp_path))
    (tmp_path / "a.txt").write_text("changed\n")

    async def boom(**kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr("server.goal_run.code_review.dual_axis_review", boom)
    task = TaskNode(
        id="t1", title="x", instruction="x", acceptance=[], depends_on=[], assignee="hicode"
    )
    result = await _run_dual_axis_review(task, str(tmp_path), before)
    assert result is None
