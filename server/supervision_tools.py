"""MasterAgent-facing tools for the Dual/Auto Supervision Runtime.

Thin orchestration-metadata tools over :mod:`veya.supervision`. They hold no
second runtime: execution is delegated to the canonical single project entry
(``server.project_ask`` → GoalRun → hicode/dsh). No tool invents a PASS; the
report carries what actually happened and the reviewer decides.
"""

from __future__ import annotations

from typing import Any

from veya.supervision import ExternalSupervisor, MissionStore, SupervisionRouter


def _mission_executors() -> frozenset[str]:
    """Executors a mission may be pinned to — projected from ExecutorRegistry.

    The registry is the only authority for executor identity, so a retired name
    disappears from admission when the registry retires it, not because this
    module was edited.  Imported lazily: ``veya.remote`` imports this module
    while building its tool bindings, so a module-level import would cycle.
    """
    from veya.remote.executor_registry import get_executor_registry

    return frozenset(get_executor_registry().snapshot())


def _validated_actions(actions: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Validate planner-resolved canonical actions.

    These are explicit, typed decisions — the planner's *WHAT*. Nothing is
    inferred from the free-text goal here. Every entry must name a registered
    canonical tool so a malformed action fails closed at admission instead of
    at execution time.
    """
    if not actions:
        return []
    from server.tool_registry import master_tools

    validated: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("each resolved action must be a mapping")
        tool = str(action.get("tool") or "").strip()
        if not tool:
            raise ValueError("each resolved action must name a tool")
        if not master_tools.has(tool):
            raise ValueError(f"unknown canonical tool in resolved action: {tool!r}")
        arguments = action.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            raise ValueError("resolved action arguments must be a mapping")
        validated.append({"tool": tool, "arguments": dict(arguments or {})})
    return validated


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
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if executor and executor not in _mission_executors():
        raise ValueError(
            f"unsupported mission executor {executor!r}; expected one of "
            f"{sorted(_mission_executors())}"
        )
    resolved_actions = _validated_actions(actions)
    mission = _facade(project_root).create(
        goal=goal,
        supervision_mode=supervision_mode,
        workspace=workspace or project_root,
        acceptance_criteria=acceptance_criteria,
        constraints=constraints,
        characteristics=characteristics,
        executor=executor or None,
    )
    if resolved_actions:
        # The planner already decided WHAT; the execution substrate decides HOW.
        mission.policies.execution_policy["actions"] = resolved_actions
        store = MissionStore(project_root)
        store.save(mission)
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


def veya_mission_set_mode(project_root: str, mission_id: str, mode: str) -> dict[str, Any]:
    """Thin wrapper over MissionStore.set_supervision_mode (same mission_id/lineage)."""

    facade = _facade(project_root)
    mission = facade.store.set_supervision_mode(mission_id, str(mode))
    facade.store.append_event(
        mission_id, "SUPERVISION_MODE_CHANGED", {"mode": str(mission.supervision_mode)}
    )
    return {"mission": mission.to_dict()}


def veya_reviews(project_root: str, mission_id: str) -> dict[str, Any]:
    """Read-only review history (canonical store, oldest first)."""

    store = MissionStore(project_root)
    return {"reviews": [review.to_dict() for review in store.reviews(mission_id)]}


def veya_events(project_root: str, mission_id: str) -> dict[str, Any]:
    """Read-only mission event log (canonical store, oldest first)."""

    store = MissionStore(project_root)
    return {"events": store.events(mission_id)}


def supervise_task(
    project_root: str,
    objective: str,
    report_every_minutes: int = 30,
    stall_after_minutes: int = 20,
    max_repairs: int = 3,
    bot_id: str | None = None,
    dispatch: bool | None = None,
) -> dict[str, Any]:
    """Start a supervised long task (flag VEYA_SUPERVISOR=1).

    W12-A2: binds one new GoalRun to one new routine and records the
    binding. The GoalRun store stays the execution authority; the
    routine registry stays the schedule authority. This function adds
    only the bookkeeping — no new scheduler, event store or approval
    path.

    Returns the binding plus the ids of the created GoalRun and
    routine so the caller (model) can tell the user what was started.
    """

    from server.supervisor import binding as supervisor_binding
    from server.supervisor import enabled as supervisor_enabled

    if not supervisor_enabled():
        raise RuntimeError(
            "supervisor is disabled; set VEYA_SUPERVISOR=1 to enable"
        )

    import uuid as _uuid
    from datetime import UTC, datetime

    from server.goal_run.bot_identity import current_bot_id
    from server.goal_run.models import GoalRunState, GoalStatus, RoutineSpec
    from server.goal_run.routine_registry import RoutineRegistry

    # Owner context: an explicit bot wins, else the bot that called this tool.
    bot_id = (str(bot_id).strip() if bot_id else "") or current_bot_id() or "veya-default"

    # The shell, the binding and the dispatched run must all use one absolute,
    # writable root (a relative/"." root is the read-only /app in prod).
    from server.goal_run.paths import resolve_goal_run_root

    project_root = resolve_goal_run_root(project_root, bot_id=bot_id)

    # 1. Create the GoalRun state (the execution authority).
    goal_id = f"goal_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{_uuid.uuid4().hex[:12]}"
    state = GoalRunState(
        goal_id=goal_id,
        goal_text=objective,
        status=GoalStatus.running,
        bot_id=bot_id,
    )
    from server.goal_run.store import save_goal_run

    save_goal_run(state, project_root)

    # 2. Create the routine that fires the report cadence (the schedule
    #    authority). trigger_topic is the structural match the routine
    #    clock already dispatches on.
    routine_id = f"supervise_{_uuid.uuid4().hex[:8]}"
    routine = RoutineSpec(
        routine_id=routine_id,
        trigger_topic="schedule.trigger",
        objective=objective,
        goal_run_id=goal_id,
        bot_id=bot_id,
        goal_template=objective,
        schedule=f"every {report_every_minutes}m",
        probe={"kind": "none"},
    )
    registry = RoutineRegistry(bot_id=bot_id)
    registry.register(routine)

    # 3. Record the binding (the bookkeeping projection).
    binding = supervisor_binding.SupervisorBinding(
        binding_id=f"sup_{_uuid.uuid4().hex[:8]}",
        goal_run_id=goal_id,
        routine_id=routine_id,
        owner_id=bot_id,
        bot_id=bot_id,
        objective=objective,
        project_root=project_root,
        report_every_minutes=int(report_every_minutes),
        stall_after_minutes=int(stall_after_minutes),
        max_repairs=int(max_repairs),
        last_activity_at=None,
    )
    supervisor_binding.save_binding(binding)

    # 4. Actually start the run. Before funcfix the GoalRun was only recorded
    #    (status=running, started_at=None, no tasks) and nothing ever executed
    #    it, so the supervisor reported "stuck" forever.
    dispatched = False
    if _dispatch_enabled() if dispatch is None else dispatch:
        dispatched = _dispatch_supervised_run(
            project_root=project_root,
            objective=objective,
            goal_run_id=goal_id,
            bot_id=bot_id,
            binding_id=binding.binding_id,
        )

    return {
        "dispatched": dispatched,
        "binding": binding.to_dict(),
        "goal_run_id": goal_id,
        "routine_id": routine_id,
        "report_every_minutes": int(report_every_minutes),
        "stall_after_minutes": int(stall_after_minutes),
        "max_repairs": int(max_repairs),
    }


def _dispatch_enabled() -> bool:
    import os

    return os.environ.get("VEYA_SUPERVISOR_DISPATCH", "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


_SUPERVISED_RUNS: set[Any] = set()


async def _run_supervised_goal(
    *, project_root: str, objective: str, goal_run_id: str, bot_id: str, binding_id: str
) -> Any:
    """Plan and execute the pre-registered GoalRun through the canonical entry."""

    import logging

    from server.coordinator_master import master_coordinator
    from server.goal_run.bot_identity import bind_current_bot, reset_current_bot
    from server.models.execution import (
        CanonicalExecutionRequest,
        ExecutionConstraints,
        ExecutionMode,
    )

    req = CanonicalExecutionRequest(
        source="ROUTINE",
        mode=ExecutionMode.STRUCTURED_CONSTRAINED,
        objective=objective,
        project_root=project_root,
        constraints=ExecutionConstraints(
            metadata={
                "bot_id": bot_id,
                "resume_goal_id": goal_run_id,
                "supervisor_binding_id": binding_id,
            }
        ),
    )
    token = bind_current_bot(bot_id)
    try:
        return await master_coordinator.execute_structured(req)
    except Exception:
        logging.getLogger(__name__).exception(
            "supervised GoalRun %s failed to execute", goal_run_id
        )
        return None
    finally:
        reset_current_bot(token)


def _dispatch_supervised_run(**kwargs: Any) -> bool:
    """Fire-and-forget the supervised run on the current loop (or a thread)."""

    import asyncio
    import threading

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(_run_supervised_goal(**kwargs))
        _SUPERVISED_RUNS.add(task)
        task.add_done_callback(_SUPERVISED_RUNS.discard)
        return True
    thread = threading.Thread(
        target=lambda: asyncio.run(_run_supervised_goal(**kwargs)),
        name=f"supervised-{kwargs.get('goal_run_id')}",
        daemon=True,
    )
    thread.start()
    return True


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
                    # No static enum: admissible executors are an ExecutorRegistry
                    # projection, and this schema is built at import time — resolving
                    # the registry here would re-enter this module while it is still
                    # initialising. Admission is enforced at call time below.
                    "description": (
                        "Pin the execution plane for this Mission; must be an executor "
                        "ExecutorRegistry still knows (empty = canonical entry decides)."
                    ),
                },
                "actions": {
                    "type": "array",
                    "description": (
                        "Planner-resolved canonical actions (the planner's WHAT). Each is "
                        "executed by the canonical L0 substrate through ActionGateway and "
                        "PermissionEngine, with no additional model call. Omit when the "
                        "planner has not resolved the work."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "arguments": {"type": "object"},
                        },
                        "required": ["tool"],
                    },
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
    (
        "veya_mission_set_mode",
        "切换 Mission 监督模式（external/internal/auto），保持同一 mission_id 与 lineage。",
        {
            "type": "object",
            "properties": {
                "project_root": {"type": "string"},
                "mission_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["external", "internal", "auto"]},
            },
            "required": ["project_root", "mission_id", "mode"],
        },
        veya_mission_set_mode,
        "local_write",
    ),
    (
        "veya_reviews",
        "读取 Mission 的 review 历史（只读，canonical store）。",
        {
            "type": "object",
            "properties": {"project_root": {"type": "string"}, "mission_id": {"type": "string"}},
            "required": ["project_root", "mission_id"],
        },
        veya_reviews,
        "pure_read",
    ),
    (
        "veya_events",
        "读取 Mission 的事件流（只读，canonical store）。",
        {
            "type": "object",
            "properties": {"project_root": {"type": "string"}, "mission_id": {"type": "string"}},
            "required": ["project_root", "mission_id"],
        },
        veya_events,
        "pure_read",
    ),
    (
        "supervise_task",
        "启动一个受监督的长任务：绑定 GoalRun + routine，按间隔汇报进度，卡住时自动续跑。仅 VEYA_SUPERVISOR=1 可用。",
        {
            "type": "object",
            "properties": {
                "project_root": {"type": "string"},
                "objective": {"type": "string", "description": "要完成的目标（中文/英文均可）"},
                "report_every_minutes": {
                    "type": "integer",
                    "default": 30,
                    "description": "进度汇报间隔（分钟）",
                },
                "stall_after_minutes": {
                    "type": "integer",
                    "default": 20,
                    "description": "多少分钟无活动判定为卡住",
                },
                "max_repairs": {
                    "type": "integer",
                    "default": 3,
                    "description": "最大自动续跑次数",
                },
            },
            "required": ["project_root", "objective"],
        },
        supervise_task,
        "local_write",
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


__all__ = ["register_tools", "supervise_task"]
