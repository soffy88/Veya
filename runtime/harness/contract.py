"""Coding task harness contract creation and persistence."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from runtime.coding.models import CodingWorkspace
from runtime.coding.worktree import validate_task_id

from .guides import load_guides
from .models import CodingHarnessContract, ProjectGuide, Sensor
from .sensors import sensors_for_workspace


def contract_path(project_root: str | Path, task_id: str) -> Path:
    validate_task_id(task_id)
    root = Path(project_root).expanduser().resolve()
    return root / ".veya" / "runs" / task_id / "inputs" / "harness_contract.json"


def build_coding_harness_contract(
    workspace: CodingWorkspace | str | Path,
    task_id: str,
    *,
    guides: Iterable[ProjectGuide] | None = None,
    sensors: Iterable[Sensor] | None = None,
    permission_profile: str = "DEVELOPMENT",
    observability_profile: str = "coding_default",
    memory_scope: str | None = None,
) -> CodingHarnessContract:
    """Build the contract that binds one coding task to its harness context."""
    validate_task_id(task_id)
    if isinstance(workspace, CodingWorkspace):
        workspace_obj = workspace
    else:
        from runtime.coding.workspace_detect import detect_workspace

        workspace_obj = detect_workspace(workspace)
    guide_list = list(guides) if guides is not None else load_guides(workspace_obj)
    sensor_list = (
        list(sensors) if sensors is not None else sensors_for_workspace(workspace_obj, guide_list)
    )
    return CodingHarnessContract(
        workspace_id=workspace_obj.id,
        guide_refs=[guide.source_path for guide in guide_list],
        required_sensors=[sensor.id for sensor in sensor_list if sensor.required],
        optional_sensors=[sensor.id for sensor in sensor_list if not sensor.required],
        permission_profile=permission_profile,
        observability_profile=observability_profile,
        memory_scope=memory_scope or f"workspace:{workspace_obj.id}",
        artifact_policy=f".veya/runs/{task_id}/outputs",
    )


def write_coding_harness_contract(
    project_root: str | Path,
    task_id: str,
    contract: CodingHarnessContract,
) -> Path:
    """Persist a contract under task inputs using an atomic local write."""
    target = contract_path(project_root, task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(contract.to_dict(), ensure_ascii=False, indent=2) + "\n"
    if target.exists() and target.read_text(encoding="utf-8") == encoded:
        return target
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(target)
    return target


def read_coding_harness_contract(
    project_root: str | Path, task_id: str
) -> CodingHarnessContract | None:
    target = contract_path(project_root, task_id)
    if not target.is_file():
        return None
    try:
        raw: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
        return CodingHarnessContract(
            workspace_id=str(raw["workspace_id"]),
            guide_refs=[str(item) for item in raw.get("guide_refs", [])],
            required_sensors=[str(item) for item in raw.get("required_sensors", [])],
            optional_sensors=[str(item) for item in raw.get("optional_sensors", [])],
            permission_profile=str(raw["permission_profile"]),
            observability_profile=str(raw["observability_profile"]),
            memory_scope=str(raw["memory_scope"]),
            artifact_policy=str(raw["artifact_policy"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


__all__ = [
    "build_coding_harness_contract",
    "contract_path",
    "read_coding_harness_contract",
    "write_coding_harness_contract",
]
