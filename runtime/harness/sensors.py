"""Computational sensor registry for coding harness verification."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.coding.command_runner import (
    CommandRunner,
    command_may_use_network,
    command_requires_approval,
    parse_command,
)
from runtime.coding.models import CodingWorkspace
from runtime.coding.worktree import WorktreeError, repo_root_for_worktree

from .guides import guide_commands, load_guides
from .models import GuideCommands, ProjectGuide, Sensor, SensorResult


class SensorRegistryError(ValueError):
    """A sensor registration or execution request is invalid."""


_KIND_ORDER = {
    "lint": 0,
    "test": 1,
    "typecheck": 2,
    "build": 3,
    "schema": 4,
    "security": 5,
    "llm_judge": 6,
}
_COST_ORDER = {"free": 0, "low": 1, "medium": 2, "high": 3}


def _workspace_root(value: str | Path | CodingWorkspace) -> tuple[Path, str]:
    root_value = value.root_path if isinstance(value, CodingWorkspace) else value
    root = Path(root_value).expanduser().resolve()
    if not root.is_dir():
        raise SensorRegistryError(f"workspace path is not a directory: {root}")
    workspace_id = (
        value.id
        if isinstance(value, CodingWorkspace)
        else ("workspace-" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12])
    )
    return root, workspace_id


def sensor_id_for(workspace_id: str, kind: str, command: str | None) -> str:
    seed = f"{workspace_id}:{kind}:{command or ''}".encode()
    return "sensor-" + hashlib.sha256(seed).hexdigest()[:16]


def _sensor_kind(command: str, field: str | None = None) -> str:
    if field in {"lint", "test", "typecheck", "build"}:
        return field
    if field == "format":
        return "lint"
    lower = command.lower()
    if any(
        word in lower
        for word in (
            "pytest",
            "npm test",
            "pnpm test",
            "yarn test",
            "bun test",
            "cargo test",
            "go test",
        )
    ):
        return "test"
    if any(word in lower for word in ("ruff", "eslint", "clippy", "go vet")):
        return "lint"
    if any(word in lower for word in ("mypy", "tsc", "typecheck", "type-check", "cargo check")):
        return "typecheck"
    if any(word in lower for word in ("build", "cargo build", "go build")):
        return "build"
    if lower.startswith(("pnpm check", "npm run check", "yarn check", "bun check")):
        return "schema"
    return "security"


def _sensor_name(command: str, kind: str) -> str:
    executable = command.split(maxsplit=1)[0] if command else kind
    return executable.strip("`") or kind


def _sensor_cost(command: str, kind: str, field: str | None) -> str:
    lower = command.lower()
    if field == "format":
        return "medium"
    if any(
        marker in lower
        for marker in (
            "personal_agent_gold",
            "tests/goal_run",
            "goal_run",
            "pnpm --dir apps/web build",
            "npm --prefix apps/web run build",
        )
    ):
        return "high"
    if kind == "test" and lower.strip() in {
        "pytest",
        "npm test",
        "pnpm test",
        "yarn test",
        "bun test",
        "cargo test",
        "go test ./...",
    }:
        return "high"
    if kind in {"build", "typecheck"} or "tests/runtime" in lower:
        return "medium"
    return "low"


def _make_sensor(workspace_id: str, command: str, *, field: str | None, required: bool) -> Sensor:
    kind = _sensor_kind(command, field)
    cost = _sensor_cost(command, kind, field)
    return Sensor(
        id=sensor_id_for(workspace_id, kind, command),
        name=_sensor_name(command, kind),
        kind=kind,  # type: ignore[arg-type]
        command=command,
        deterministic=True,
        cost_level=cost,  # type: ignore[arg-type]
        required=required,
        timeout_s=900,
    )


def _custom_sensor_path(root: Path) -> Path:
    return root / ".veya" / "harness" / "sensors.json"


def load_custom_sensors(root: str | Path) -> list[Sensor]:
    path = _custom_sensor_path(Path(root).expanduser().resolve())
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SensorRegistryError(f"invalid persisted sensor registry: {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise SensorRegistryError(f"persisted sensor registry must be a list: {path}")
    sensors: list[Sensor] = []
    for item in raw:
        if isinstance(item, dict):
            sensors.append(Sensor(**item))
    return sensors


def persist_sensor(root: str | Path, sensor: Sensor) -> Path:
    project_root = Path(root).expanduser().resolve()
    path = _custom_sensor_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = {item.id: item for item in load_custom_sensors(project_root)}
    current[sensor.id] = sensor
    encoded = json.dumps(
        [item.to_dict() for item in sorted(current.values(), key=lambda item: item.id)],
        ensure_ascii=False,
        indent=2,
    )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(encoded + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


class SensorRegistry:
    """In-memory registry with explicit registration and deterministic ordering."""

    def __init__(self, sensors: Iterable[Sensor] = ()) -> None:
        self._sensors: dict[str, Sensor] = {}
        for sensor in sensors:
            self.register(sensor)

    def register(self, sensor: Sensor, *, replace: bool = False) -> Sensor:
        if not sensor.id or not sensor.name:
            raise SensorRegistryError("sensor id and name are required")
        if sensor.timeout_s <= 0:
            raise SensorRegistryError("sensor timeout_s must be positive")
        if sensor.command:
            try:
                parse_command(sensor.command)
            except ValueError as exc:
                raise SensorRegistryError(f"sensor command is invalid: {sensor.id}: {exc}") from exc
        if sensor.id in self._sensors and not replace:
            raise SensorRegistryError(f"sensor already registered: {sensor.id}")
        self._sensors[sensor.id] = sensor
        return sensor

    def get(self, sensor_id: str) -> Sensor | None:
        return self._sensors.get(sensor_id)

    def list(self) -> list[Sensor]:
        return sorted(
            self._sensors.values(),
            key=lambda item: (
                _KIND_ORDER.get(item.kind, 99),
                _COST_ORDER.get(item.cost_level, 99),
                item.id,
            ),
        )


def sensors_for_workspace(
    workspace: CodingWorkspace | str | Path,
    guides: Iterable[ProjectGuide] | None = None,
    *,
    include_persisted: bool = True,
) -> list[Sensor]:
    """Infer computational sensors from workspace metadata and project guides."""
    root, workspace_id = _workspace_root(workspace)
    workspace_obj = workspace if isinstance(workspace, CodingWorkspace) else None
    if workspace_obj is None:
        from runtime.coding.workspace_detect import detect_workspace

        workspace_obj = detect_workspace(root)
    guide_list = list(guides) if guides is not None else load_guides(workspace_obj)
    guide_cmds: GuideCommands = guide_commands(guide_list)
    guide_values = guide_cmds.all()
    registry = SensorRegistry()
    for field in ("test_commands", "lint_commands", "typecheck_commands", "build_commands"):
        guide_field = field.removesuffix("_commands")
        inferred_commands = [] if guide_values[guide_field] else getattr(workspace_obj, field)
        for command in inferred_commands:
            registry.register(
                _make_sensor(
                    workspace_id, command, field=field.removesuffix("_commands"), required=True
                )
            )
    for field in ("test", "lint", "typecheck", "build", "format"):
        commands = guide_values[field]
        for command in commands:
            registry.register(
                _make_sensor(
                    workspace_id,
                    command,
                    field=field,
                    # FORMAT is useful evidence, but it is not one of the
                    # acceptance-required sensor classes.  This also lets a
                    # protected user change remain a visible optional result.
                    required=field != "format",
                ),
                replace=True,
            )
    # These are useful when a guide explicitly asks for a schema/build probe
    # but workspace command inference did not find a package script.
    if (root / "package.json").is_file():
        package_manager = workspace_obj.package_manager or "npm"
        package_data: Mapping[str, Any] = {}
        try:
            loaded = json.loads((root / "package.json").read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                package_data = loaded
        except (OSError, json.JSONDecodeError):
            package_data = {}
        scripts_raw = package_data.get("scripts")
        scripts = scripts_raw if isinstance(scripts_raw, dict) else {}
        if "check" in scripts:
            command = f"{package_manager} check"
            registry.register(
                _make_sensor(workspace_id, command, field=None, required=True), replace=True
            )
    if include_persisted:
        for sensor in load_custom_sensors(root):
            registry.register(sensor, replace=True)
    return registry.list()


def _task_id_for_worktree(path: Path) -> str:
    if not path.name.startswith("task-"):
        raise WorktreeError("sensor execution requires a task worktree")
    return path.name.removeprefix("task-")


def run_sensor(
    sensor: Sensor,
    worktree_path: str | Path,
    *,
    profile: str = "local_restricted",
    approved: bool = False,
    run_id: str | None = None,
    require_worktree: bool = True,
) -> SensorResult:
    """Run one sensor through the existing coding command runner."""
    target = Path(worktree_path).expanduser().resolve()
    if not target.is_dir():
        raise SensorRegistryError(f"sensor target is not a directory: {target}")
    if require_worktree:
        repo_root_for_worktree(target)
    task_id = run_id or (_task_id_for_worktree(target) if require_worktree else "doctor")
    if Path(task_id).name != task_id or task_id in {"", ".", ".."}:
        raise SensorRegistryError("sensor run id must be a safe path component")
    evidence_id = "sensor-evidence-" + uuid.uuid4().hex[:12]
    started_at = datetime.now(UTC).isoformat()
    if not sensor.command:
        completed_at = datetime.now(UTC).isoformat()
        return SensorResult(
            sensor_id=sensor.id,
            status="skipped",
            exit_code=None,
            output_ref=None,
            evidence_ids=[evidence_id],
            duration_ms=0,
            message="sensor has no command; evidence is insufficient",
            command=None,
            required=sensor.required,
            deterministic=sensor.deterministic,
            started_at=started_at,
            completed_at=completed_at,
        )
    runner = CommandRunner(
        target,
        profile=profile,
        artifact_root=target / ".veya" / "runs" / task_id / "outputs",
    )
    result = runner.run(sensor.command, cwd=target, timeout_s=sensor.timeout_s, approved=approved)
    if result.status == "passed":
        status = "passed"
    elif result.status == "failed":
        status = "failed"
    elif result.status in {"approval_required", "denied"}:
        status = "error"
    else:
        status = "error"
    completed_at = datetime.now(UTC).isoformat()
    return SensorResult(
        sensor_id=sensor.id,
        status=status,  # type: ignore[arg-type]
        exit_code=result.exit_code,
        output_ref=result.artifact_path,
        evidence_ids=[evidence_id],
        duration_ms=round(result.duration_ms),
        message=result.stderr or result.stdout,
        command=result.command,
        required=sensor.required,
        deterministic=sensor.deterministic,
        started_at=started_at,
        completed_at=completed_at,
    )


def sensor_acceptance(
    sensors: Iterable[Sensor],
    results: Iterable[SensorResult | Mapping[str, Any]],
) -> dict[str, Any]:
    sensor_list = list(sensors)
    result_map: dict[str, SensorResult | Mapping[str, Any]] = {
        str(item.sensor_id if isinstance(item, SensorResult) else item.get("sensor_id")): item
        for item in results
    }
    failures: list[str] = []
    insufficient: list[str] = []
    for sensor in sensor_list:
        if not sensor.required:
            continue
        result = result_map.get(sensor.id)
        status = (
            result.status
            if isinstance(result, SensorResult)
            else str(result.get("status"))
            if result
            else None
        )
        if result is None or status == "skipped":
            insufficient.append(sensor.id)
        elif status != "passed":
            failures.append(sensor.id)
    return {
        "acceptance_passed": not failures and not insufficient,
        "required_failures": failures,
        "insufficient_evidence": insufficient,
    }


def sensor_command_is_safe(sensor: Sensor) -> tuple[bool, str]:
    if not sensor.command:
        return True, "no command"
    try:
        argv = parse_command(sensor.command)
    except ValueError as exc:
        return False, str(exc)
    if command_requires_approval(argv):
        return False, "command requires approval (explicit approval is required)"
    if command_may_use_network(argv):
        return False, "command may use network; doctor sensors are offline-only"
    return True, "safe under command policy"


__all__ = [
    "SensorRegistry",
    "SensorRegistryError",
    "load_custom_sensors",
    "persist_sensor",
    "run_sensor",
    "sensor_acceptance",
    "sensor_command_is_safe",
    "sensor_id_for",
    "sensors_for_workspace",
]
