from __future__ import annotations

import json
import subprocess
from pathlib import Path

from runtime.coding.workspace_detect import detect_workspace
from runtime.harness.doctor import run_harness_doctor
from runtime.harness.sensors import sensors_for_workspace


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "harness@example.invalid")
    _git(root, "config", "user.name", "Harness Tests")
    (root / ".gitignore").write_text(".veya/\n", encoding="utf-8")
    (root / "README.md").write_text("harness\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


def _doctor_kwargs():
    return {
        "permissions_explicit": True,
        "gold_gate_status": "PASS",
        "health_status": "PASS",
        "direct_io_clean": True,
    }


def test_no_guide_is_degraded_not_blocked(tmp_path: Path):
    root = _repo(tmp_path)
    (root / ".veya" / "runs").mkdir(parents=True)

    report = run_harness_doctor(root, unattended=True, **_doctor_kwargs())

    assert report.status == "HARNESS_DEGRADED"
    assert any("no supported guide" in reason for reason in report.degraded_reasons)


def test_missing_test_command_is_degraded(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text(
        "## Commands\n- build: python3 -c \"print('build')\"\n- lint: python3 -c \"print('lint')\"\n"
        "## Permissions\n- Permission and approval policy is explicit.\n",
        encoding="utf-8",
    )
    (root / ".veya" / "runs").mkdir(parents=True)

    report = run_harness_doctor(root, **_doctor_kwargs())

    assert report.status == "HARNESS_DEGRADED"
    assert any("guide test command" in reason for reason in report.degraded_reasons)


def test_all_required_sensors_pass_is_ready(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text(
        """## Commands
- build: python3 -c "print('build')"
- test: python3 -c "print('test')"
- lint: python3 -c "print('lint')"
## Permissions
- Permission and approval policy is explicit.
""",
        encoding="utf-8",
    )
    (root / ".veya" / "runs").mkdir(parents=True)
    sensors = sensors_for_workspace(detect_workspace(root))
    results = [{"sensor_id": sensor.id, "status": "passed"} for sensor in sensors]

    report = run_harness_doctor(root, sensor_results=results, **_doctor_kwargs())

    assert report.status == "HARNESS_READY"
    assert not report.blockers


def test_unsafe_sensor_command_blocks_doctor(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text(
        """## Commands
- build: python3 -c "print('build')"
- test: rm -rf /
- lint: python3 -c "print('lint')"
## Permissions
- Permission and approval policy is explicit.
""",
        encoding="utf-8",
    )
    (root / ".veya" / "runs").mkdir(parents=True)

    report = run_harness_doctor(root, **_doctor_kwargs())

    assert report.status == "HARNESS_BLOCKED"
    assert any("requires approval" in blocker for blocker in report.blockers)


def test_quick_sensor_run_persists_and_rereads_evidence(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text(
        """## BUILD
- build: python3 -c "print('build')"
## TEST
- test: python3 -c "print('test')"
## LINT
- lint: python3 -c "print('lint')"
## PERMISSIONS
- Explicit permission and approval policy applies.
""",
        encoding="utf-8",
    )

    report = run_harness_doctor(
        root,
        run_sensors=True,
        sensor_mode="quick",
        doctor_run_id="doctor-persistence",
        **_doctor_kwargs(),
    )

    assert report.status == "HARNESS_READY"
    assert report.evidence_run_id == "doctor-persistence"
    assert report.sensor_report_path is not None
    assert report.verification_report_path is not None
    sensor_report = json.loads(Path(report.sensor_report_path).read_text(encoding="utf-8"))
    verification_report = json.loads(
        Path(report.verification_report_path).read_text(encoding="utf-8")
    )
    assert sensor_report["run_id"] == "doctor-persistence"
    assert sensor_report["results"]
    required_fields = {
        "run_id",
        "workspace_id",
        "guide_sources",
        "sensor_id",
        "command",
        "status",
        "exit_code",
        "duration_ms",
        "output_artifact",
        "required",
        "deterministic",
        "started_at",
        "completed_at",
    }
    assert required_fields <= set(sensor_report["results"][0])
    assert verification_report["acceptance_passed"] is True
    assert (root / ".veya/runs/doctor-persistence/artifact_manifest.json").is_file()


def test_required_failed_sensor_is_blocked_from_persisted_evidence(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text(
        """## BUILD
- build: python3 -c "print('build')"
## TEST
- test: python3 -c "import sys; sys.exit(1)"
## LINT
- lint: python3 -c "print('lint')"
## PERMISSIONS
- Explicit permission and approval policy applies.
""",
        encoding="utf-8",
    )

    report = run_harness_doctor(
        root,
        run_sensors=True,
        sensor_mode="quick",
        doctor_run_id="doctor-failure",
        **_doctor_kwargs(),
    )

    assert report.status == "HARNESS_BLOCKED"
    assert report.sensor_report_path is not None
    sensor_report = json.loads(Path(report.sensor_report_path).read_text(encoding="utf-8"))
    assert sensor_report["acceptance_passed"] is False
    assert any(item["status"] == "failed" for item in sensor_report["results"])
