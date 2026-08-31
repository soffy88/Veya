"""Coding task finalization — artifact generation and verification.

This module handles the finalization phase of a coding task:
- Generate diff.patch
- Write sensor_report.json
- Write verification_report.json
- Write artifact_manifest.json
- Write final_result.json
"""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.execution.models import DelegateResult


def _atomic_write(path: Path, content: str) -> Path:
    """Atomically write content to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return path


def _atomic_write_json(path: Path, data: dict[str, Any]) -> Path:
    """Atomically write JSON to path."""
    return _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def generate_diff_patch(
    worktree_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Generate diff.patch from worktree changes."""
    output_dir.mkdir(parents=True, exist_ok=True)
    patch_path = output_dir / "diff.patch"

    try:
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        diff_content = result.stdout
        if result.returncode != 0 and result.stderr:
            diff_content = f"# git diff error: {result.stderr}\n{diff_content}"
    except subprocess.TimeoutExpired:
        diff_content = "# git diff timed out\n"
    except Exception as e:
        diff_content = f"# git diff failed: {type(e).__name__}: {e}\n"

    _atomic_write(patch_path, diff_content)

    # Also generate changed_files.json
    changed_files: list[str] = []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            changed_files = [f for f in result.stdout.strip().split("\n") if f]
    except Exception:
        pass

    changed_files_path = output_dir / "changed_files.json"
    _atomic_write_json(changed_files_path, {"files": changed_files})

    return {
        "patch_path": str(patch_path),
        "changed_files": changed_files,
        "diff_size": len(diff_content),
    }


def generate_sensor_report(
    output_dir: Path,
    sensor_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Generate sensor_report.json from sensor execution results."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "sensor_report.json"

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "sensors": sensor_results,
        "summary": {
            "total": len(sensor_results),
            "passed": sum(1 for s in sensor_results if s.get("status") == "passed"),
            "failed": sum(1 for s in sensor_results if s.get("status") == "failed"),
            "skipped": sum(1 for s in sensor_results if s.get("status") == "skipped"),
        },
    }

    _atomic_write_json(path, report)
    return {"path": str(path), "summary": report["summary"]}


def generate_verification_report(
    output_dir: Path,
    task_id: str,
    delegate_result: DelegateResult,
    sensor_results: list[dict[str, Any]],
    test_results: dict[str, Any] | None = None,
    lint_results: dict[str, Any] | None = None,
    typecheck_results: dict[str, Any] | None = None,
    build_results: dict[str, Any] | None = None,
    artifact_count: int | None = None,
) -> dict[str, Any]:
    """Generate verification_report.json."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "verification_report.json"

    report_id = f"vr_{uuid.uuid4().hex[:12]}"

    # Determine overall acceptance
    required_sensors = [s for s in sensor_results if s.get("required", True)]
    required_passed = all(s.get("status") == "passed" for s in required_sensors)

    # Check if all acceptance results passed
    acceptance_results_passed = all(
        ar.status == "passed" for ar in delegate_result.acceptance_results if ar.required
    )

    acceptance_passed = (
        delegate_result.status == "complete" and required_passed and acceptance_results_passed
    )

    report = {
        "id": report_id,
        "task_id": task_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "delegate_status": delegate_result.status,
        "delegate_stop_reason": delegate_result.stop_reason,
        "acceptance_passed": acceptance_passed,
        "acceptance_results": [ar.to_dict() for ar in delegate_result.acceptance_results],
        "sensor_summary": {
            "total": len(sensor_results),
            "passed": sum(1 for s in sensor_results if s.get("status") == "passed"),
            "failed": sum(1 for s in sensor_results if s.get("status") == "failed"),
            "required_passed": required_passed,
        },
        "tests": test_results or {},
        "lint": lint_results or {},
        "typecheck": typecheck_results or {},
        "build": build_results or {},
        "evidence_count": len(delegate_result.evidence),
        "artifact_count": (
            len(delegate_result.artifacts) if artifact_count is None else artifact_count
        ),
        "side_effect_count": 0,  # DelegateResult doesn't track side effects directly
    }

    _atomic_write_json(path, report)
    return {"id": report_id, "path": str(path), "acceptance_passed": acceptance_passed}


def generate_artifact_manifest(
    output_dir: Path,
    task_id: str,
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Generate artifact_manifest.json."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "artifact_manifest.json"

    manifest = {
        "task_id": task_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "artifacts": artifacts,
        "count": len(artifacts),
    }

    _atomic_write_json(path, manifest)
    return {"path": str(path), "count": len(artifacts)}


def generate_final_result(
    output_dir: Path,
    task_id: str,
    goal_run_id: str | None,
    objective: str,
    status: str,
    delegate_result: DelegateResult,
    verification_report_id: str | None,
    artifact_ids: list[str],
    changed_files: list[str],
    test_results: dict[str, Any] | None = None,
    lint_results: dict[str, Any] | None = None,
    typecheck_results: dict[str, Any] | None = None,
    build_results: dict[str, Any] | None = None,
    known_failures: list[str] | None = None,
) -> dict[str, Any]:
    """Generate final_result.json."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "final_result.json"

    # Calculate acceptance_passed from acceptance_results
    acceptance_passed = all(
        ar.status == "passed" for ar in delegate_result.acceptance_results if ar.required
    )

    result = {
        "task_id": task_id,
        "goal_run_id": goal_run_id,
        "status": status,
        "objective": objective,
        "files_changed": changed_files,
        "tests": test_results or {},
        "lint": lint_results or {},
        "typecheck": typecheck_results or {},
        "build": build_results or {},
        "acceptance_passed": acceptance_passed,
        "known_failures": known_failures or [],
        "artifact_ids": artifact_ids,
        "verification_report_id": verification_report_id,
        "summary": delegate_result.summary,
        "stop_reason": delegate_result.stop_reason,
        "generated_at": datetime.now(UTC).isoformat(),
    }

    _atomic_write_json(path, result)
    return result


def finalize_coding_task(
    project_root: Path,
    task_id: str,
    goal_run_id: str | None,
    objective: str,
    worktree_path: Path,
    delegate_result: DelegateResult,
    sensor_results: list[dict[str, Any]],
    test_results: dict[str, Any] | None = None,
    lint_results: dict[str, Any] | None = None,
    typecheck_results: dict[str, Any] | None = None,
    build_results: dict[str, Any] | None = None,
    known_failures: list[str] | None = None,
) -> dict[str, Any]:
    """Run full finalization for a coding task.

    Generates all required artifacts:
    - diff.patch
    - changed_files.json
    - sensor_report.json
    - verification_report.json
    - artifact_manifest.json
    - final_result.json
    """
    output_dir = project_root / ".veya" / "runs" / task_id / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Generate diff
    diff_info = generate_diff_patch(worktree_path, output_dir)

    # 2. Generate sensor report
    generate_sensor_report(output_dir, sensor_results)

    # The finalizer owns the required product artifacts.  Delegate leaves may
    # contribute additional artifacts, but an empty delegate artifact list
    # must not make the completed verification appear artifact-free.
    core_artifact_names = (
        ("diff.patch", "diff"),
        ("changed_files.json", "changed_files"),
        ("sensor_report.json", "sensor_report"),
        ("verification_report.json", "verification_report"),
        ("final_result.json", "final_result"),
    )
    delegate_artifact_paths = {str(artifact.path) for artifact in delegate_result.artifacts}
    core_artifact_count = sum(
        1
        for name, _kind in core_artifact_names
        if str(output_dir / name) not in delegate_artifact_paths
    )

    # 3. Generate verification report
    verification_info = generate_verification_report(
        output_dir,
        task_id,
        delegate_result,
        sensor_results,
        test_results=test_results,
        lint_results=lint_results,
        typecheck_results=typecheck_results,
        build_results=build_results,
        artifact_count=len(delegate_result.artifacts) + core_artifact_count,
    )

    # 4. Materialize the final result before writing the manifest so the
    # manifest is a complete index of all user-visible finalization outputs.
    final_status = (
        "completed"
        if delegate_result.status == "complete" and verification_info["acceptance_passed"]
        else (
            "partial_completed"
            if delegate_result.status == "partial"
            else "failed"
            if delegate_result.status == "failed"
            else "partial_completed"
        )
    )
    artifacts = [
        {"id": f"art_{i}_{uuid.uuid4().hex[:8]}", "path": str(a.path)}
        for i, a in enumerate(delegate_result.artifacts)
    ]
    next_index = len(artifacts)
    for name, kind in core_artifact_names:
        path = output_dir / name
        if any(str(item.get("path")) == str(path) for item in artifacts):
            continue
        artifacts.append(
            {
                "id": f"art_{next_index}_{uuid.uuid4().hex[:8]}",
                "path": str(path),
                "kind": kind,
                "status": "verified" if verification_info["acceptance_passed"] else "partial",
            }
        )
        next_index += 1

    generate_final_result(
        output_dir,
        task_id,
        goal_run_id,
        objective,
        final_status,
        delegate_result,
        verification_info["id"],
        [a["id"] for a in artifacts],
        diff_info["changed_files"],
        test_results=test_results,
        lint_results=lint_results,
        typecheck_results=typecheck_results,
        build_results=build_results,
        known_failures=known_failures,
    )

    # 5. Generate artifact manifest last; every listed path now exists.
    generate_artifact_manifest(output_dir, task_id, artifacts)

    return {
        "status": final_status,
        "verification_report_id": verification_info["id"],
        "artifact_ids": [a["id"] for a in artifacts],
        "changed_files": diff_info["changed_files"],
        "acceptance_passed": verification_info["acceptance_passed"],
        "outputs": {
            "diff_patch": str(output_dir / "diff.patch"),
            "changed_files": str(output_dir / "changed_files.json"),
            "sensor_report": str(output_dir / "sensor_report.json"),
            "verification_report": str(output_dir / "verification_report.json"),
            "artifact_manifest": str(output_dir / "artifact_manifest.json"),
            "final_result": str(output_dir / "final_result.json"),
        },
    }


__all__ = [
    "finalize_coding_task",
    "generate_artifact_manifest",
    "generate_diff_patch",
    "generate_final_result",
    "generate_sensor_report",
    "generate_verification_report",
]
