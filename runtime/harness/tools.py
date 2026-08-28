"""MasterAgent-facing tools for querying and operating the harness layer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from runtime.coding.workspace_detect import detect_workspace
from runtime.coding.worktree import repo_root_for_worktree

from .guides import guide_commands, guide_conflicts, load_guides, search_guides, show_guide
from .ratchet import RatchetStore, apply_candidate, transition_candidate
from .sensors import run_sensor, sensor_acceptance, sensors_for_workspace


def _result(
    status: str,
    *,
    data: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    requires_approval: bool = False,
    side_effect: bool = False,
) -> dict[str, Any]:
    return {
        "status": status,
        "data": data or {},
        "evidence": evidence or [],
        "artifacts": artifacts or [],
        "command_results": [],
        "side_effect": side_effect,
        "requires_approval": requires_approval,
    }


def _failed(message: str, *, requires_approval: bool = False) -> dict[str, Any]:
    return _result(
        "failed",
        evidence=[{"kind": "error", "message": message}],
        requires_approval=requires_approval,
    )


def harness_guides_load(path: str = ".") -> dict[str, Any]:
    try:
        guides = load_guides(path)
        conflicts = guide_conflicts(guides)
    except Exception as exc:
        return _failed(f"guide load failed: {type(exc).__name__}: {exc}")
    return _result(
        "ok",
        data={
            "guides": [guide.to_dict() for guide in guides],
            "conflicts": [conflict.to_dict() for conflict in conflicts],
            "commands": guide_commands(guides).to_dict(),
        },
        evidence=[
            {
                "kind": "project_guides_loaded",
                "count": len(guides),
                "conflicts": len(conflicts),
                "source_paths": [guide.source_path for guide in guides],
            }
        ],
    )


def harness_guides_search(path: str, query: str) -> dict[str, Any]:
    try:
        matches = search_guides(path, query)
    except Exception as exc:
        return _failed(f"guide search failed: {type(exc).__name__}: {exc}")
    return _result(
        "ok",
        data={"query": query, "matches": matches},
        evidence=[{"kind": "project_guide_search", "query": query, "match_count": len(matches)}],
    )


def harness_guides_show(path: str, source_path: str | None = None) -> dict[str, Any]:
    try:
        shown = show_guide(path, source_path=source_path)
    except Exception as exc:
        return _failed(f"guide show failed: {type(exc).__name__}: {exc}")
    shown_guides = shown.get("guides")
    return _result(
        "ok",
        data=shown,
        evidence=[
            {
                "kind": "project_guide_shown",
                "source_path": source_path,
                "guide_count": len(shown_guides) if isinstance(shown_guides, list) else 0,
            }
        ],
    )


def _workspace_and_sensors(path: str) -> tuple[Any, list[Any]]:
    workspace = detect_workspace(path)
    guides = load_guides(workspace)
    return workspace, sensors_for_workspace(workspace, guides)


def harness_sensor_list(path: str = ".") -> dict[str, Any]:
    try:
        workspace, sensors = _workspace_and_sensors(path)
    except Exception as exc:
        return _failed(f"sensor list failed: {type(exc).__name__}: {exc}")
    return _result(
        "ok",
        data={
            "workspace_id": workspace.id,
            "required": [sensor.to_dict() for sensor in sensors if sensor.required],
            "optional": [sensor.to_dict() for sensor in sensors if not sensor.required],
            "sensors": [sensor.to_dict() for sensor in sensors],
        },
        evidence=[{"kind": "sensor_registry_listed", "count": len(sensors)}],
    )


def harness_sensor_run(
    worktree_path: str,
    sensor_id: str,
    profile: str = "local_restricted",
    approved: bool = False,
) -> dict[str, Any]:
    try:
        root = repo_root_for_worktree(worktree_path)
        _workspace, sensors = _workspace_and_sensors(str(root))
        sensor = next((item for item in sensors if item.id == sensor_id), None)
        if sensor is None:
            return _failed(f"sensor not found in workspace: {sensor_id}")
        result = run_sensor(sensor, worktree_path, profile=profile, approved=approved)
    except Exception as exc:
        return _failed(f"sensor run failed: {type(exc).__name__}: {exc}")
    status = "ok" if result.status == "passed" else "failed"
    return _result(
        status,
        data={"sensor": sensor.to_dict(), "sensor_result": result.to_dict()},
        evidence=[{"kind": "sensor_result", **result.to_dict()}],
        artifacts=[{"path": result.output_ref, "kind": "sensor_output"}]
        if result.output_ref
        else [],
        requires_approval=result.status == "error" and "approval" in result.message.lower(),
        side_effect=True,
    )


def harness_sensor_report(
    path: str,
    results: list[Mapping[str, Any]],
) -> dict[str, Any]:
    try:
        workspace, sensors = _workspace_and_sensors(path)
        report = sensor_acceptance(sensors, results)
    except Exception as exc:
        return _failed(f"sensor report failed: {type(exc).__name__}: {exc}")
    status = "ok" if report["acceptance_passed"] else "failed"
    if report["insufficient_evidence"] and not report["required_failures"]:
        status = "partial"
    return _result(
        status,
        data={"workspace_id": workspace.id, "sensor_results": results, **report},
        evidence=[{"kind": "sensor_acceptance", **report}],
    )


def harness_ratchet_candidates(path: str = ".", status: str | None = None) -> dict[str, Any]:
    try:
        candidates = RatchetStore(path).list()
        if status:
            candidates = [candidate for candidate in candidates if candidate.status == status]
    except Exception as exc:
        return _failed(f"Ratchet candidate list failed: {type(exc).__name__}: {exc}")
    return _result(
        "ok",
        data={"candidates": [candidate.to_dict() for candidate in candidates]},
        evidence=[{"kind": "ratchet_candidates_listed", "count": len(candidates)}],
    )


def harness_ratchet_approve(path: str, candidate_id: str) -> dict[str, Any]:
    try:
        candidate = transition_candidate(path, candidate_id, "approved")
    except Exception as exc:
        return _failed(f"Ratchet approval failed: {type(exc).__name__}: {exc}")
    return _result(
        "ok",
        data={"candidate": candidate.to_dict()},
        evidence=[{"kind": "ratchet_candidate_approved", "candidate_id": candidate.id}],
        side_effect=True,
    )


def harness_ratchet_reject(path: str, candidate_id: str) -> dict[str, Any]:
    try:
        candidate = transition_candidate(path, candidate_id, "rejected")
    except Exception as exc:
        return _failed(f"Ratchet rejection failed: {type(exc).__name__}: {exc}")
    return _result(
        "ok",
        data={"candidate": candidate.to_dict()},
        evidence=[{"kind": "ratchet_candidate_rejected", "candidate_id": candidate.id}],
        side_effect=True,
    )


def harness_ratchet_apply(path: str, candidate_id: str) -> dict[str, Any]:
    try:
        candidate = apply_candidate(path, candidate_id)
    except Exception as exc:
        return _failed(f"Ratchet apply failed: {type(exc).__name__}: {exc}")
    return _result(
        "ok",
        data={"candidate": candidate.to_dict()},
        evidence=[
            {
                "kind": "ratchet_candidate_applied",
                "candidate_id": candidate.id,
                "applied_path": candidate.applied_path,
            }
        ],
        side_effect=True,
    )


def register_tools(registry: Any) -> int:
    """Register harness query/verification tools into the existing registry."""
    from server.tool_registry import SideEffect

    common_path = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "workspace path"}},
    }
    tools = [
        (
            "harness_guides_load",
            "读取项目 guides 作为带来源的 project context。",
            common_path,
            harness_guides_load,
            SideEffect.PURE_READ,
        ),
        (
            "harness_guides_search",
            "按关键词查询项目 guide 规则并返回 source path/line。",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}, "query": {"type": "string"}},
                "required": ["path", "query"],
            },
            harness_guides_search,
            SideEffect.PURE_READ,
        ),
        (
            "harness_guides_show",
            "显示项目 guides、命令、反模式和冲突。",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}, "source_path": {"type": "string"}},
                "required": ["path"],
            },
            harness_guides_show,
            SideEffect.PURE_READ,
        ),
        (
            "harness_sensor_list",
            "列出 workspace 推断出的 computational sensors。",
            common_path,
            harness_sensor_list,
            SideEffect.PURE_READ,
        ),
        (
            "harness_sensor_run",
            "在隔离 coding worktree 中运行一个 sensor 并返回证据。",
            {
                "type": "object",
                "properties": {
                    "worktree_path": {"type": "string"},
                    "sensor_id": {"type": "string"},
                    "profile": {"type": "string"},
                    "approved": {"type": "boolean"},
                },
                "required": ["worktree_path", "sensor_id"],
            },
            harness_sensor_run,
            SideEffect.PROCESS_EXEC,
        ),
        (
            "harness_sensor_report",
            "根据 SensorResult 计算 required sensor acceptance 和 insufficient evidence。",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}, "results": {"type": "array"}},
                "required": ["path", "results"],
            },
            harness_sensor_report,
            SideEffect.PURE_READ,
        ),
        (
            "harness_ratchet_candidates",
            "列出 evidence-backed Ratchet candidates；candidate 不等于 applied rule。",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}, "status": {"type": "string"}},
            },
            harness_ratchet_candidates,
            SideEffect.PURE_READ,
        ),
        (
            "harness_ratchet_approve",
            "批准一个 Ratchet candidate，但不自动写入 guide/sensor。",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}, "candidate_id": {"type": "string"}},
                "required": ["path", "candidate_id"],
            },
            harness_ratchet_approve,
            SideEffect.LOCAL_WRITE,
        ),
        (
            "harness_ratchet_reject",
            "拒绝一个 Ratchet candidate，使其不可应用。",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}, "candidate_id": {"type": "string"}},
                "required": ["path", "candidate_id"],
            },
            harness_ratchet_reject,
            SideEffect.LOCAL_WRITE,
        ),
        (
            "harness_ratchet_apply",
            "应用已经批准的 guide/sensor candidate；未批准或无证据一律拒绝。",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}, "candidate_id": {"type": "string"}},
                "required": ["path", "candidate_id"],
            },
            harness_ratchet_apply,
            SideEffect.LOCAL_WRITE,
        ),
    ]
    added = 0
    for name, description, parameters, function, side_effect in tools:
        if registry.has(name):
            continue
        registry.register(
            name,
            description,
            parameters,
            function,
            max_result_chars=30000,
            side_effect=side_effect,
            effect_capability="manual_only" if side_effect is not SideEffect.PURE_READ else "none",
        )
        added += 1
    return added


__all__ = [
    "harness_guides_load",
    "harness_guides_search",
    "harness_guides_show",
    "harness_ratchet_apply",
    "harness_ratchet_approve",
    "harness_ratchet_candidates",
    "harness_ratchet_reject",
    "harness_sensor_list",
    "harness_sensor_report",
    "harness_sensor_run",
    "register_tools",
]
