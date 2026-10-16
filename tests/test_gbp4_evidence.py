"""GBP4 Evidence feature tests (VEYA_EVIDENCE_REPORT).

Tests instruction updates and structured task completion reports (Source / Guess / Skip)
with automated URL/file extraction from evidence chains.
"""

from __future__ import annotations

from pathlib import Path
import pytest

from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus
from server.supervisor.reporting import build_progress_report
from server.supervisor.binding import SupervisorBinding


def test_evidence_report_flag_off_behavior(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("VEYA_EVIDENCE_REPORT", "0")
    
    # Check default bot instructions
    from server.routes.bot_team import _default_bot_spec
    spec = _default_bot_spec("alice")
    assert "说不清数字怎么来的就不写" not in spec.instructions

    # Check role preset instructions via render_role_block
    from server.role_skill_presets import load_roles, render_role_block
    roles = load_roles()
    if roles:
        block = render_role_block(roles[0], budget=2000)
        assert "说不清数字怎么来的就不写" not in block

    # Check progress report does not force Source/Guess/Skip structure
    state = GoalRunState(goal_id="g1", goal_text="test goal", status=GoalStatus.completed)
    state.tasks["t1"] = TaskNode(
        id="t1",
        title="task 1",
        instruction="do task 1",
        acceptance=["ok"],
        depends_on=[],
        assignee="worker",
        status=TaskStatus.completed,
        evidence=[{"content": "Fetched https://example.com/data and file server/supervisor/reporting.py"}]
    )
    from server.goal_run.store import save_goal_run
    save_goal_run(state, str(tmp_path))

    binding = SupervisorBinding(
        binding_id="b1",
        routine_id="r1",
        goal_run_id="g1",
        owner_id="alice",
        bot_id="veya-default",
        project_root=str(tmp_path),
        objective="test goal",
        report_seq=1,
    )
    report = build_progress_report(goal_run_id="g1", project_root=str(tmp_path), binding=binding)
    assert "【来源】" not in report["body"]
    assert "【猜测】" not in report["body"]
    assert "【跳过】" not in report["body"]


def test_evidence_report_flag_on_behavior_and_structure(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("VEYA_EVIDENCE_REPORT", "1")

    # 1. Check default bot instructions
    from server.routes.bot_team import _default_bot_spec
    spec = _default_bot_spec("alice")
    assert "说不清数字怎么来的就不写" in spec.instructions

    # 2. Check role preset instructions via render_role_block
    from server.role_skill_presets import load_roles, render_role_block
    roles = load_roles()
    if roles:
        block = render_role_block(roles[0], budget=2000)
        assert "说不清数字怎么来的就不写" in block

    # 3. Check progress report structure (Source / Guess / Skip) with real evidence chain URL
    state = GoalRunState(goal_id="g2", goal_text="analyze api", status=GoalStatus.completed)
    state.tasks["t1"] = TaskNode(
        id="t1",
        title="fetch api",
        instruction="do fetch",
        acceptance=["ok"],
        depends_on=[],
        assignee="worker",
        status=TaskStatus.completed,
        evidence=[
            {"content": "Successfully connected to https://api.veya.ai/v1/health and read server/supervisor/reporting.py"}
        ]
    )
    from server.goal_run.store import save_goal_run
    save_goal_run(state, str(tmp_path))

    binding = SupervisorBinding(
        binding_id="b2",
        routine_id="r2",
        goal_run_id="g2",
        owner_id="alice",
        bot_id="veya-default",
        project_root=str(tmp_path),
        objective="analyze api",
        report_seq=1,
    )
    report = build_progress_report(goal_run_id="g2", project_root=str(tmp_path), binding=binding)
    
    body = report["body"]
    assert "【来源】" in body
    assert "【猜测】" in body
    assert "【跳过】" in body
    assert "https://api.veya.ai/v1/health" in body
    assert "server/supervisor/reporting.py" in body
