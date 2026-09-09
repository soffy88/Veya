"""Regression tests for the coding_task_run GoalRun plan contract."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from server.tools.coding_task import _build_goal_run_task, _prepare_worktree_dependencies


def test_worktree_dependencies_are_exposed_without_copying_trees(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    worktree_path = project_root / ".veya" / "worktrees" / "task-1"
    for relative_path in (Path("venv"), Path("node_modules"), Path("apps/web/node_modules")):
        (project_root / relative_path).mkdir(parents=True)
    worktree_path.mkdir(parents=True)

    _prepare_worktree_dependencies(project_root, worktree_path)

    for relative_path in (Path("venv"), Path("node_modules"), Path("apps/web/node_modules")):
        target = worktree_path / relative_path
        assert target.is_symlink()
        assert target.resolve() == (project_root / relative_path).resolve()


def test_goal_run_plan_contains_only_unexecuted_verification_contract(tmp_path: Path) -> None:
    sensors = [
        SimpleNamespace(id="sensor-test", kind="test", command="pytest -q tests"),
        SimpleNamespace(id="sensor-lint", kind="lint", command="ruff check ."),
    ]

    task = _build_goal_run_task(
        task_id="task-1",
        objective="verify the coding task",
        worktree_path=tmp_path / "worktree",
        contract_path=tmp_path / "inputs" / "harness_contract.json",
        output_dir=tmp_path / "outputs",
        sensors=sensors,
    )

    instruction = task["instruction"]
    acceptance = "\n".join(task["acceptance"])
    serialized = f"{instruction}\n{acceptance}"

    assert task["assignee"] == "hicode"
    assert "1 passed, 0 failed" not in serialized
    assert "acceptance_passed" not in serialized
    assert "candidates only, not pre-executed results" in instruction
    assert "identify every required sensor" in instruction
    assert "primary test suite is available" in instruction
    assert "explicit skipped reason for every required check not selected" in instruction
    assert "sensor-test (test): pytest -q tests" in instruction
    assert "sensor-lint (lint): ruff check ." in instruction
    for artifact in (
        "verification_report.json",
        "sensor_report.json",
        "changed_files.json",
        "final_result.json",
        "artifact_manifest.json",
    ):
        assert artifact in instruction
