"""Tests for coding task verification and acceptance.

Tests:
VERIFY-01 all required pass
VERIFY-02 required fail
VERIFY-03 skipped required
VERIFY-04 artifact manifest
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


@pytest.mark.asyncio
async def test_verify_01_all_required_pass(test_repo: Path) -> None:
    """VERIFY-01: All required sensors pass → acceptance_passed=True."""
    from runtime.coding.finalize import finalize_coding_task
    from runtime.execution.models import DelegateResult, Evidence

    # Create delegate result with all passing
    delegate_result = DelegateResult(
        delegate_id="test-verify-01",
        status="complete",
        summary="All tests passed",
        stop_reason="completed",
        evidence=[
            Evidence(id="ev-1", kind="test", source="pytest", content="passed", producer="test")
        ],
        artifacts=[],
        assertions=[],
        acceptance_results=[],
    )

    # All sensors passed
    sensor_results = [
        {"id": "test", "name": "pytest", "status": "passed", "required": True},
        {"id": "lint", "name": "ruff", "status": "passed", "required": True},
    ]

    result = finalize_coding_task(
        project_root=test_repo,
        task_id="verify-01",
        goal_run_id=None,
        objective="Test all pass",
        worktree_path=test_repo,
        delegate_result=delegate_result,
        sensor_results=sensor_results,
    )

    assert result["status"] == "completed"
    assert result["acceptance_passed"] is True


@pytest.mark.asyncio
async def test_verify_02_required_fail(test_repo: Path) -> None:
    """VERIFY-02: Required sensor fails → acceptance_passed=False, partial_completed."""
    from runtime.coding.finalize import finalize_coding_task
    from runtime.execution.models import DelegateResult, Evidence

    # Create delegate result
    delegate_result = DelegateResult(
        delegate_id="test-verify-02",
        status="partial",
        summary="Tests failed",
        stop_reason="acceptance_failed",
        evidence=[
            Evidence(id="ev-1", kind="test", source="pytest", content="failed", producer="test")
        ],
        artifacts=[],
        assertions=[],
        acceptance_results=[],
    )

    # One required sensor failed
    sensor_results = [
        {
            "id": "test",
            "name": "pytest",
            "status": "failed",
            "required": True,
            "error": "assertion failed",
        },
        {"id": "lint", "name": "ruff", "status": "passed", "required": True},
    ]

    result = finalize_coding_task(
        project_root=test_repo,
        task_id="verify-02",
        goal_run_id=None,
        objective="Test required fail",
        worktree_path=test_repo,
        delegate_result=delegate_result,
        sensor_results=sensor_results,
    )

    assert result["status"] == "partial_completed"
    assert result["acceptance_passed"] is False


@pytest.mark.asyncio
async def test_verify_03_skipped_required(test_repo: Path) -> None:
    """VERIFY-03: Skipped required sensor → acceptance_passed=False."""
    from runtime.coding.finalize import finalize_coding_task
    from runtime.execution.models import DelegateResult

    # Create delegate result
    delegate_result = DelegateResult(
        delegate_id="test-verify-03",
        status="partial",
        summary="Sensor skipped",
        stop_reason="acceptance_failed",
        evidence=[],
        artifacts=[],
        assertions=[],
        acceptance_results=[],
    )

    # One required sensor skipped
    sensor_results = [
        {
            "id": "test",
            "name": "pytest",
            "status": "skipped",
            "required": True,
            "error": "command not found",
        },
        {"id": "lint", "name": "ruff", "status": "passed", "required": True},
    ]

    result = finalize_coding_task(
        project_root=test_repo,
        task_id="verify-03",
        goal_run_id=None,
        objective="Test skipped required",
        worktree_path=test_repo,
        delegate_result=delegate_result,
        sensor_results=sensor_results,
    )

    assert result["status"] == "partial_completed"
    assert result["acceptance_passed"] is False


@pytest.mark.asyncio
async def test_verify_04_artifact_manifest(test_repo: Path) -> None:
    """VERIFY-04: Artifact manifest is generated correctly."""
    from runtime.coding.finalize import generate_artifact_manifest

    # Generate artifact manifest
    outputs_dir = test_repo / ".veya" / "runs" / "verify-04" / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    artifacts = [
        {"id": "art-1", "path": str(outputs_dir / "diff.patch")},
        {"id": "art-2", "path": str(outputs_dir / "verification_report.json")},
    ]

    result = generate_artifact_manifest(outputs_dir, "verify-04", artifacts)

    assert result["count"] == 2

    # Verify file exists
    manifest_path = outputs_dir / "artifact_manifest.json"
    assert manifest_path.exists()

    # Verify content
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_data["task_id"] == "verify-04"
    assert len(manifest_data["artifacts"]) == 2


@pytest.mark.asyncio
async def test_sensor_report_generation(test_repo: Path) -> None:
    """Test sensor report is generated correctly."""
    from runtime.coding.finalize import generate_sensor_report

    outputs_dir = test_repo / ".veya" / "runs" / "sensor-test" / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    sensor_results = [
        {
            "id": "test",
            "name": "pytest",
            "status": "passed",
            "required": True,
            "output": "5 passed",
        },
        {
            "id": "lint",
            "name": "ruff",
            "status": "failed",
            "required": True,
            "error": "unused import",
        },
        {"id": "typecheck", "name": "mypy", "status": "skipped", "required": False},
    ]

    result = generate_sensor_report(outputs_dir, sensor_results)

    assert result["summary"]["total"] == 3
    assert result["summary"]["passed"] == 1
    assert result["summary"]["failed"] == 1
    assert result["summary"]["skipped"] == 1

    # Verify file
    report_path = outputs_dir / "sensor_report.json"
    assert report_path.exists()


@pytest.mark.asyncio
async def test_verification_report_structure(test_repo: Path) -> None:
    """Test verification report has correct structure."""
    from runtime.coding.finalize import generate_verification_report
    from runtime.execution.models import DelegateResult

    outputs_dir = test_repo / ".veya" / "runs" / "vr-test" / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    delegate_result = DelegateResult(
        delegate_id="test-vr",
        status="complete",
        summary="Test",
        stop_reason="completed",
        evidence=[],
        artifacts=[],
        assertions=[],
        acceptance_results=[],
    )

    sensor_results = [
        {"id": "test", "status": "passed", "required": True},
    ]

    result = generate_verification_report(
        outputs_dir,
        "vr-test",
        delegate_result,
        sensor_results,
        test_results={"passed": 5, "failed": 0},
    )

    assert "id" in result
    assert result["acceptance_passed"] is True

    # Verify file
    vr_path = outputs_dir / "verification_report.json"
    assert vr_path.exists()

    vr_data = json.loads(vr_path.read_text(encoding="utf-8"))
    assert vr_data["task_id"] == "vr-test"
    assert "sensor_summary" in vr_data
    assert "tests" in vr_data


@pytest.mark.asyncio
async def test_final_result_structure(test_repo: Path) -> None:
    """Test final_result.json has correct structure."""
    from runtime.coding.finalize import generate_final_result
    from runtime.execution.models import DelegateResult

    outputs_dir = test_repo / ".veya" / "runs" / "fr-test" / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    delegate_result = DelegateResult(
        delegate_id="test-fr",
        status="complete",
        summary="All done",
        stop_reason="completed",
        evidence=[],
        artifacts=[],
        assertions=[],
        acceptance_results=[],
    )

    result = generate_final_result(
        outputs_dir,
        "fr-test",
        "gr-123",
        "Test objective",
        "completed",
        delegate_result,
        "vr-456",
        ["art-1"],
        ["main.py"],
        test_results={"passed": 5},
        lint_results={"errors": 0},
    )

    assert result["task_id"] == "fr-test"
    assert result["goal_run_id"] == "gr-123"
    assert result["status"] == "completed"
    assert result["acceptance_passed"] is True
    assert result["files_changed"] == ["main.py"]
    assert result["verification_report_id"] == "vr-456"

    # Verify file
    fr_path = outputs_dir / "final_result.json"
    assert fr_path.exists()


@pytest.mark.asyncio
async def test_diff_patch_generation(test_repo: Path) -> None:
    """Test diff.patch is generated."""
    from runtime.coding.finalize import generate_diff_patch

    outputs_dir = test_repo / ".veya" / "runs" / "diff-test" / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    # Modify a file
    (test_repo / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    result = generate_diff_patch(test_repo, outputs_dir)

    assert "patch_path" in result
    assert "changed_files" in result

    # Verify patch file
    patch_path = outputs_dir / "diff.patch"
    assert patch_path.exists()

    # Verify changed files
    changed_files_path = outputs_dir / "changed_files.json"
    assert changed_files_path.exists()


__all__ = [
    "test_diff_patch_generation",
    "test_final_result_structure",
    "test_sensor_report_generation",
    "test_verification_report_structure",
    "test_verify_01_all_required_pass",
    "test_verify_02_required_fail",
    "test_verify_03_skipped_required",
    "test_verify_04_artifact_manifest",
]
