"""MasterAgent-facing tools for the Dual/Auto Supervision Runtime.

Thin orchestration-metadata tools over :mod:`veya.supervision`. They hold no
second runtime: execution is delegated to the canonical single project entry
(``server.project_ask`` → GoalRun → hicode/dsh). No tool invents a PASS; the
report carries what actually happened and the reviewer decides.
"""

from __future__ import annotations

from typing import Any

from veya.supervision import ExternalSupervisor, MissionStore, SupervisionRouter


async def _default_runner(mission: Any) -> Any:
    """Canonical execution entry: the single project dispatch (builtin/hicode/dsh)."""

    from veya.supervision.runner import canonical_runner

    return await canonical_runner(mission)


def _facade(project_root: str, *, with_runner: bool = False) -> ExternalSupervisor:
    store = MissionStore(project_root)
    return ExternalSupervisor(
        store=store,
        router=SupervisionRouter(store),
        runner=_default_runner if with_runner else None,
    )


def veya_mission_create(
    project_root: str,
    goal: str,
    supervision_mode: str = "auto",
    workspace: str = "",
    acceptance_criteria: list[str] | None = None,
    constraints: list[str] | None = None,
    characteristics: list[str] | None = None,
    executor: str = "",
) -> dict[str, Any]:
    mission = _facade(project_root).create(
        goal=goal,
        supervision_mode=supervision_mode,
        workspace=workspace or project_root,
        acceptance_criteria=acceptance_criteria,
        constraints=constraints,
        characteristics=characteristics,
        executor=executor or None,
    )
    return {"mission": mission.to_dict()}


def veya_mission_inspect(project_root: str, mission_id: str) -> dict[str, Any]:
    return _facade(project_root).inspect(mission_id)


async def veya_mission_run(project_root: str, mission_id: str) -> dict[str, Any]:
    return await _facade(project_root, with_runner=True).run(mission_id)


def veya_mission_continue(
    project_root: str, mission_id: str, review: dict[str, Any]
) -> dict[str, Any]:
    """Apply a supervisor review and retask (also the external reconnect path)."""

    return _facade(project_root).apply_review(mission_id, review)


def veya_mission_cancel(project_root: str, mission_id: str) -> dict[str, Any]:
    return _facade(project_root).cancel(mission_id)


def veya_report_latest(project_root: str, mission_id: str) -> dict[str, Any]:
    return {"report": _facade(project_root).latest_report(mission_id)}


def veya_report_get(project_root: str, mission_id: str, iteration: int) -> dict[str, Any]:
    return {"report": _facade(project_root).get_report(mission_id, int(iteration))}


def veya_review_apply(project_root: str, mission_id: str, review: dict[str, Any]) -> dict[str, Any]:
    return _facade(project_root).apply_review(mission_id, review)


def veya_escalation_list(project_root: str, mission_id: str) -> dict[str, Any]:
    return {"escalations": _facade(project_root).list_escalations(mission_id)}


_TOOLS: tuple[tuple[str, str, dict[str, Any], Any, Any], ...] = (
    (
        "veya_mission_create",
        "创建 Mission（orchestration metadata）。mode: external=ChatGPT监督 / internal=Veya自主 / auto=自动。",
        {
            "type": "object",
            "properties": {
                "project_root": {"type": "string"},
                "goal": {"type": "string"},
                "supervision_mode": {"type": "string", "enum": ["external", "internal", "auto"]},
                "workspace": {"type": "string"},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "characteristics": {"type": "array", "items": {"type": "string"}},
                "executor": {
                    "type": "string",
                    "enum": ["hicode", "dsh", "worker", "builtin", "native_tool"],
                    "description": "Pin the execution plane for this Mission (empty = canonical entry decides).",
                },
            },
            "required": ["project_root", "goal"],
        },
        veya_mission_create,
        "local_write",
    ),
    (
        "veya_mission_inspect",
        "查看 Mission 当前状态、supervisor、lineage、最新 report 与 review。",
        {
            "type": "object",
            "properties": {"project_root": {"type": "string"}, "mission_id": {"type": "string"}},
            "required": ["project_root", "mission_id"],
        },
        veya_mission_inspect,
        "pure_read",
    ),
    (
        "veya_mission_run",
        "执行一轮 Mission（走 canonical 项目入口 → GoalRun → hicode/dsh），并生成 ExecutionReport。",
        {
            "type": "object",
            "properties": {"project_root": {"type": "string"}, "mission_id": {"type": "string"}},
            "required": ["project_root", "mission_id"],
        },
        veya_mission_run,
        "process_exec",
    ),
    (
        "veya_mission_continue",
        "应用一次 SupervisorReview 并 retask；external 模式下也是 reconnect 后的续跑入口。",
        {
            "type": "object",
            "properties": {
                "project_root": {"type": "string"},
                "mission_id": {"type": "string"},
                "review": {"type": "object"},
            },
            "required": ["project_root", "mission_id", "review"],
        },
        veya_mission_continue,
        "local_write",
    ),
    (
        "veya_mission_cancel",
        "取消 Mission（不改写已有 report/review 记录）。",
        {
            "type": "object",
            "properties": {"project_root": {"type": "string"}, "mission_id": {"type": "string"}},
            "required": ["project_root", "mission_id"],
        },
        veya_mission_cancel,
        "local_write",
    ),
    (
        "veya_report_latest",
        "读取 Mission 最新一轮 ExecutionReport（Supervisor review 的输入）。",
        {
            "type": "object",
            "properties": {"project_root": {"type": "string"}, "mission_id": {"type": "string"}},
            "required": ["project_root", "mission_id"],
        },
        veya_report_latest,
        "pure_read",
    ),
    (
        "veya_report_get",
        "按 iteration 读取 ExecutionReport。",
        {
            "type": "object",
            "properties": {
                "project_root": {"type": "string"},
                "mission_id": {"type": "string"},
                "iteration": {"type": "integer"},
            },
            "required": ["project_root", "mission_id", "iteration"],
        },
        veya_report_get,
        "pure_read",
    ),
    (
        "veya_review_apply",
        "提交 SupervisorReview（decision ∈ ACCEPT/CONTINUE/REVISE/RETRY/ROLLBACK/ESCALATE/DONE）。",
        {
            "type": "object",
            "properties": {
                "project_root": {"type": "string"},
                "mission_id": {"type": "string"},
                "review": {"type": "object"},
            },
            "required": ["project_root", "mission_id", "review"],
        },
        veya_review_apply,
        "local_write",
    ),
    (
        "veya_escalation_list",
        "列出 Mission 的升级事件（仅 owner-only 条件会出现在这里）。",
        {
            "type": "object",
            "properties": {"project_root": {"type": "string"}, "mission_id": {"type": "string"}},
            "required": ["project_root", "mission_id"],
        },
        veya_escalation_list,
        "pure_read",
    ),
)


def register_tools(registry: Any) -> int:
    """Register supervision tools into the existing MasterToolRegistry."""

    from server.tool_registry import SideEffect

    effects = {
        "pure_read": SideEffect.PURE_READ,
        "local_write": SideEffect.LOCAL_WRITE,
        "process_exec": SideEffect.PROCESS_EXEC,
    }
    added = 0
    for name, description, parameters, func, effect in _TOOLS:
        if registry.has(name):
            continue
        registry.register(
            name,
            description,
            parameters,
            func,
            max_result_chars=30000,
            side_effect=effects[effect],
            effect_capability="none" if effect == "pure_read" else "manual_only",
        )
        added += 1
    return added


__all__ = ["register_tools"]
