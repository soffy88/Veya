"""Regression: a GoalRun with no project root must not write into a read-only cwd.

Prod 2026-10-13: the S6 routine fired every 2 minutes from cwd ``/app`` (read-only)
and every run died in ``planner.py`` with ``PermissionError: '.veya-project'``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from server.goal_run.paths import resolve_goal_run_root
from server.goal_run.planner import g1_plan

_TASKS = [
    {
        "id": "t1",
        "title": "report the time",
        "instruction": "report the current time",
        "acceptance": ["a time is reported"],
    }
]


@pytest.fixture
def readonly_cwd(tmp_path, monkeypatch):
    ro = tmp_path / "app"
    ro.mkdir()
    write_root = tmp_path / "work"
    monkeypatch.setenv("VEYA_WRITE_ROOT", str(write_root))
    monkeypatch.chdir(ro)
    ro.chmod(stat.S_IRUSR | stat.S_IXUSR)
    if os.access(ro, os.W_OK):  # running as root: permissions are not enforced
        ro.chmod(0o755)
        pytest.skip("cannot make a read-only directory as this user")
    yield ro, write_root
    ro.chmod(0o755)


@pytest.mark.parametrize("given", [None, "", "."])
def test_missing_root_falls_back_to_write_root(readonly_cwd, given):
    ro, write_root = readonly_cwd
    resolved = Path(resolve_goal_run_root(given, bot_id="w11_s6_x"))
    assert resolved == write_root / "goal_run" / "w11_s6_x"
    assert resolved.is_dir()
    assert ro not in resolved.parents


def test_writable_cwd_is_kept(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_goal_run_root(None) == str(tmp_path.resolve())


def test_absolute_root_is_kept(tmp_path):
    assert resolve_goal_run_root(str(tmp_path)) == str(tmp_path)


async def test_g1_plan_with_readonly_cwd_writes_under_write_root(readonly_cwd):
    ro, write_root = readonly_cwd
    state, _resp = await g1_plan(
        interpretation="report the time",
        assumptions=[],
        goal_text="report the time",
        project_root=None,
        explicit_tasks=_TASKS,
        bot_id="w11_s6_x",
    )
    run_dir = write_root / "goal_run" / "w11_s6_x" / ".veya-project" / "goal-runs" / state.goal_id
    assert (run_dir / "taskgraph.json").is_file()
    assert not (ro / ".veya-project").exists()
