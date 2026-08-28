from __future__ import annotations

import subprocess
from pathlib import Path

from runtime.coding.workspace_detect import detect_workspace
from runtime.harness.models import Sensor, SensorResult
from runtime.harness.sensors import (
    SensorRegistry,
    sensor_acceptance,
    sensors_for_workspace,
)


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


def test_registry_registers_pytest_and_ruff(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(
        "[project]\ndependencies=['pytest','ruff']\n[tool.ruff]\nline-length=100\n",
        encoding="utf-8",
    )

    sensors = sensors_for_workspace(detect_workspace(root))
    commands = {sensor.command for sensor in sensors}

    assert "pytest" in commands
    assert "ruff check ." in commands
    assert all(sensor.deterministic for sensor in sensors)


def test_registry_registers_tsc_when_tsconfig_has_no_script(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "package.json").write_text("{}\n", encoding="utf-8")
    (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")

    sensors = sensors_for_workspace(detect_workspace(root))

    assert "tsc --noEmit" in {sensor.command for sensor in sensors}
    assert any(sensor.kind == "typecheck" for sensor in sensors)


def test_required_sensor_failure_fails_acceptance():
    sensor = Sensor("test-sensor", "pytest", "test", "pytest", True, "low", True, 60)
    result = SensorResult("test-sensor", "failed", 1, "outputs/test.json", ["e-test"], 12)

    report = sensor_acceptance([sensor], [result])

    assert report["acceptance_passed"] is False
    assert report["required_failures"] == ["test-sensor"]


def test_llm_judge_is_advisory_only():
    sensor = Sensor("judge", "judge", "llm_judge", None, True, "high", True, 60)
    registry = SensorRegistry([sensor])

    assert registry.get("judge").required is False
    assert sensor_acceptance(registry.list(), [])["acceptance_passed"] is True


def test_skipped_required_sensor_is_insufficient_evidence():
    sensor = Sensor("required", "required", "schema", "", True, "free", True, 60)
    result = SensorResult("required", "skipped", None, None, ["e-skip"], 0, "no command")

    report = sensor_acceptance([sensor], [result])

    assert report["acceptance_passed"] is False
    assert report["insufficient_evidence"] == ["required"]
