"""Canonical mission runner — the single execution leg for every supervision mode.

It delegates to the project's ONE dispatch entry (``server.project_ask`` →
GoalRun → canonical L1/L0 substrate); it never executes work itself and never
invents a verdict. The returned object is a neutral projection of what
actually ran.

Executor choice is made by *capability and runtime health*, never by name:
an executor whose provider is unavailable must not make the whole L2 plane
permanently blocked while another capable, healthy, admitted executor exists.
When no external executor qualifies, a mission that already carries
planner-resolved canonical actions executes them through the ONE existing
deterministic substrate (GoalRun → ActionGateway → PermissionEngine → L0 tool)
rather than through a second execution authority.
"""

from __future__ import annotations

import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

Dispatch = Callable[[str, str], Awaitable[str]]

# The in-process executor id. It never calls an external model provider.
_LOCAL_EXECUTOR = "builtin"

# Executors retired from the canonical registry.  This is NOT an admission list —
# it exists only so supervision can refuse a retired name explicitly instead of
# letting it fall through as an unknown worker.  The set of executors supervision
# may actually consider is answered by the registry (see ``_active_executors``).
_DEPRECATED_EXECUTOR_NAMES = frozenset({"hicode"})

#: Identity kind used for a substrate that runs in-process with no provider.
_LOCAL_SUBSTRATE_KIND = "in_process_substrate"


def _ensure_local_substrate_registered() -> None:
    """Declare the in-process substrate in the canonical ExecutorRegistry.

    ``builtin`` has no provider contract, so the registry cannot discover it.  We
    therefore *register* it through the registry's own public API rather than keep
    a supervision-side allowlist — the registry stays the single authority for
    which executors exist, and supervision owns no inventory of its own.
    """
    from veya.remote.executor_registry import ExecutorRuntimeIdentity, get_executor_registry

    registry = get_executor_registry()
    if _LOCAL_EXECUTOR in registry.snapshot():
        return
    registry.register(
        ExecutorRuntimeIdentity(
            executor_id=_LOCAL_EXECUTOR,
            executor_kind=_LOCAL_SUBSTRATE_KIND,
            provider=None,
            model=None,
            auth_state="NOT_REQUIRED",
            reachable=True,
            launcher=None,
            capabilities=frozenset(),
            runtime_source="supervision-local-substrate",
            status="READY",
        )
    )


def _active_executors() -> tuple[str, ...]:
    """Every executor the mission plane may consider — answered by the registry."""
    _ensure_local_substrate_registered()
    from veya.remote.executor_registry import get_executor_registry

    return tuple(sorted(get_executor_registry().snapshot()))


def _active_l1_executors() -> tuple[str, ...]:
    """Registry-active executors that can carry an L1 assignment.

    Derived from the registry, minus the in-process substrate: ``builtin`` runs
    planner-resolved canonical actions and is not an L1 provider worker, so plan
    validation and re-dispatch exclude it.  This projects the registry; it does
    not maintain a list of its own.
    """
    return tuple(name for name in _active_executors() if name != _LOCAL_EXECUTOR)


def executor_hint(mission: Any) -> str | None:
    """Executor pinned on the mission (``policies.execution_policy.assignee_hint``).

    The mission owns *what* to run and on which executor; ``project_ask`` still owns
    *how* to run it. No routing decision is invented here — an unknown/absent value
    simply means "let the canonical entry decide".

    Admission is the registry's answer, not a supervision-local list: a hint is
    honoured when the canonical registry knows that executor, and refused when the
    registry has retired it.  Refusing an unknown or retired name leaves the choice
    with the canonical entry rather than substituting one.
    """
    policies = getattr(mission, "policies", None)
    execution = getattr(policies, "execution_policy", None) or {}
    hint = str(execution.get("assignee_hint") or "").strip().lower()
    if not hint or hint in _DEPRECATED_EXECUTOR_NAMES:
        return None
    return hint if hint in _active_executors() else None


# internal alias kept for the runner's own call sites / tests
_assignee_hint = executor_hint


# ── executor capability / health projection ──────────────────────────────
# Every field below is READ from an existing canonical registry. This module
# owns no capability truth of its own; it only projects the registries onto the
# mission plane so selection can be capability+health aware.


@dataclass(frozen=True)
class ExecutorCandidate:
    """One registered executor as the mission plane sees it."""

    executor_id: str
    local: bool
    capability_satisfied: bool
    reachable: bool
    authenticated: bool
    health: str
    admission_supported: bool
    provider_dependency: bool

    @property
    def eligible(self) -> bool:
        """A candidate is eligible only on real, probed evidence.

        ``reachable``/``authenticated`` come from the identity registry's live
        probe, never from an optimistic default: an executor that cannot be
        reached or has no credentials is not eligible even when the in-memory
        health registry has no record of it yet (a cold registry defaults to
        HEALTHY, which would otherwise admit a dead provider).
        """
        if not self.capability_satisfied or not self.admission_supported:
            return False
        if self.local:
            return True
        return self.reachable and self.authenticated and self.health != "UNAVAILABLE"

    @property
    def qualified(self) -> bool:
        """Probed readiness: reachable and holding valid credentials."""
        return bool(self.reachable and self.authenticated)

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor_id": self.executor_id,
            "local": self.local,
            "qualified": self.qualified,
            "capability_satisfied": self.capability_satisfied,
            "reachable": self.reachable,
            "authenticated": self.authenticated,
            "health": self.health,
            "admission_supported": self.admission_supported,
            "provider_dependency": self.provider_dependency,
            "eligible": self.eligible,
        }


def _mission_capabilities() -> dict[str, Any]:
    from veya.remote.worker_runtime import capabilities_for

    return {name: capabilities_for(name) for name in _active_executors()}


def _health_registry() -> Any:
    from veya.remote.executor_health import ExecutorHealthRegistry

    return ExecutorHealthRegistry()


def executor_candidates(
    *,
    required_capabilities: dict[str, Any] | None = None,
    health_registry: Any | None = None,
    local_capable: bool = True,
) -> list[ExecutorCandidate]:
    """Project every admitted executor with its real capability and health.

    ``local_capable`` reflects whether the in-process substrate actually has
    something to do: ``builtin`` can only perform planner-resolved canonical
    actions, so a mission without them leaves it ineligible instead of handing
    it work it cannot execute.
    """
    from veya.remote.executor_health import check_capability_compatible, normalize_executor_name
    from veya.remote.executor_registry import get_executor_registry

    registry = get_executor_registry()
    health = health_registry or _health_registry()
    candidates: list[ExecutorCandidate] = []
    for name in _active_executors():
        identity = registry.identity(name)
        # Local-ness is read from the registry identity, not from a name list, so
        # supervision cannot drift from what the registry actually declares.
        local = identity.executor_kind == _LOCAL_SUBSTRATE_KIND
        candidates.append(
            ExecutorCandidate(
                executor_id=name,
                local=local,
                capability_satisfied=bool(
                    local_capable
                    if local
                    else check_capability_compatible(
                        normalize_executor_name(name), required_capabilities
                    )
                ),
                reachable=True if local else bool(getattr(identity, "reachable", False)),
                authenticated=True if local else bool(getattr(identity, "authenticated", False)),
                health="LOCAL" if local else str(health.get_health(name)),
                admission_supported=True,
                provider_dependency=not local,
            )
        )
    return candidates


def executor_inventory(*, local_capable: bool = True) -> list[dict[str, Any]]:
    """Machine-readable capability/health inventory of the admitted executors."""
    return [candidate.to_dict() for candidate in executor_candidates(local_capable=local_capable)]


def select_mission_executor(
    *,
    requested: str | None = None,
    required_capabilities: dict[str, Any] | None = None,
    health_registry: Any | None = None,
    local_capable: bool = True,
    allow_autonomous_external: bool = True,
) -> tuple[ExecutorCandidate | None, list[ExecutorCandidate]]:
    """Choose one eligible executor by capability + health, in preference order.

    ``requested`` is a *preference*, not a pin: when the preferred executor is
    not eligible (unhealthy provider, missing capability, missing credentials)
    the next eligible candidate is selected instead of blocking the mission.

    ``allow_autonomous_external=False`` keeps a mission that named no executor
    from silently acquiring a provider-backed one. Executor admission stays an
    explicit decision, so a mission with no declared executor and no
    planner-resolved action has no execution target at all.
    """
    candidates = executor_candidates(
        required_capabilities=required_capabilities,
        health_registry=health_registry,
        local_capable=local_capable,
    )
    eligible = [candidate for candidate in candidates if candidate.eligible]
    if requested:
        for candidate in eligible:
            if candidate.executor_id == requested:
                return candidate, eligible
    if requested or allow_autonomous_external:
        return (eligible[0] if eligible else None), eligible
    local_only = [candidate for candidate in eligible if candidate.local]
    return (local_only[0] if local_only else None), eligible


def _required_capabilities(mission: Any) -> dict[str, Any] | None:
    """Capabilities the mission actually needs, derived from resolved actions.

    A mission with no planner-resolved action needs no mutation capability, so
    a read-only executor stays eligible. A mission that resolved a local write
    action requires a real file-effect executor.
    """
    actions = _resolved_actions(mission)
    if not actions:
        return None
    if any(str(action.get("tool")) != "read_hashline" for action in actions):
        return {"supports_file_effect": True}
    return {"supports_read_task": True}


def _resolved_actions(mission: Any) -> list[dict[str, Any]]:
    """Planner-resolved canonical actions carried by the mission.

    This is the planner's *WHAT*. It is an explicit, typed input; nothing here
    infers an action from the free-text goal, and no external model is required
    to execute an action that is already resolved.
    """
    policies = getattr(mission, "policies", None)
    execution = getattr(policies, "execution_policy", None) or {}
    actions = execution.get("actions") or []
    return [dict(action) for action in actions if isinstance(action, dict)]


def _default_dispatch(project_root: str, request: str) -> Awaitable[str]:
    from server.project_ask import project_ask

    return project_ask(project_root=project_root, request=request)


def _dispatch_for(assignee_hint: str | None) -> Dispatch:
    """Canonical dispatch, optionally pinned to one executor."""
    if not assignee_hint:
        return _default_dispatch

    def _pinned(project_root: str, request: str) -> Awaitable[str]:
        from server.project_ask import project_ask

        return project_ask(project_root=project_root, request=request, assignee_hint=assignee_hint)

    return _pinned


async def canonical_runner(mission: Any, *, dispatch: Dispatch | None = None) -> Any:
    """Run one mission iteration through the canonical GoalRun substrate.

    A mission result is projected from the durable GoalRun state, never from
    executor prose. Executor selection is capability+health aware, so one dead
    provider cannot block the plane. When no external executor qualifies, a
    mission carrying planner-resolved actions runs them on the ONE existing
    deterministic substrate; with no such action it stays fail-closed.
    """

    hint = _assignee_hint(mission)
    if dispatch is not None:
        text = await dispatch(str(mission.workspace or ""), str(mission.goal))
        return types.SimpleNamespace(
            goal_id=None,
            status="executed",
            tasks={},
            final_summary=str(text),
            unfinished_work=[],
        )

    actions = _resolved_actions(mission)
    required = _required_capabilities(mission)
    selected, eligible = select_mission_executor(
        requested=hint,
        required_capabilities=required,
        local_capable=bool(actions),
        allow_autonomous_external=bool(hint),
    )
    if selected is None:
        return _no_execution_target(
            mission, hint=hint, eligible=eligible, has_actions=bool(actions)
        )

    result = await _run_canonical_goal(mission, assignee=selected.executor_id, actions=actions)
    return await _failover_if_provider_failure(
        mission,
        result,
        failed_executor=selected.executor_id,
        actions=actions,
        required=required,
        attempted={selected.executor_id},
    )


_PROVIDER_FAILURE_CLASSES = {"PROVIDER_UNAVAILABLE", "AUTH_FAILURE", "TRANSPORT_FAILURE"}


def _failure_class_of(result: Any) -> str | None:
    """Classify a failed iteration with the canonical executor failure taxonomy."""
    from veya.remote.executor_health import classify_executor_failure

    for source in (
        str(getattr(result, "block_reason", "") or ""),
        str(getattr(result, "final_summary", "") or ""),
    ):
        if not source:
            continue
        failure = classify_executor_failure(detail=source)
        if str(failure) in _PROVIDER_FAILURE_CLASSES:
            return str(failure)
    return None


def _replay_safe(result: Any) -> bool:
    """True when the failed attempt produced no side effect worth repeating.

    Failover re-runs the work, so it is only safe while nothing observable
    changed. The execution delta is computed by GoalRun from a real baseline
    snapshot, which makes this a fact about the workspace rather than a claim.
    """
    delta = getattr(result, "execution_delta", None)
    if isinstance(delta, dict):
        for key in ("execution_created", "execution_modified", "execution_deleted"):
            if delta.get(key):
                return False
    if int(getattr(result, "attributed_path_count", 0) or 0):
        return False
    budget = getattr(result, "budget", None)
    if isinstance(budget, dict):
        action = budget.get("last_canonical_action")
        result_body = action.get("result") if isinstance(action, dict) else None
        if isinstance(result_body, dict) and result_body.get("executed"):
            return False
    return True


_PROVIDER_FAILURE_PREFIX = "PROVIDER_"


def _record_failure_against_the_responsible_layer(failed_executor: str, failure_class: str) -> None:
    """Charge a failure to the layer that actually caused it.

    A provider refusal is not the executor's fault. Recording it against
    executor health degrades an executor that launched cleanly and was then
    turned away upstream, which then looks broken during failover selection --
    so the one layer that could have recovered the mission is the one most
    likely to have been excluded by the previous failure.
    """

    if str(failure_class).startswith(_PROVIDER_FAILURE_PREFIX):
        from veya.remote.executor_registry import get_executor_registry
        from veya.remote.provider_registry import (
            ProviderAvailability,
            ProviderHealthState,
            get_provider_registry,
            normalize_provider_name,
        )

        try:
            provider_name = get_executor_registry().identity(failed_executor).provider
        except ValueError:
            provider_name = None
        if not provider_name:
            return
        try:
            providers = get_provider_registry()
            providers.record_observation(
                normalize_provider_name(provider_name),
                health_state=str(ProviderHealthState.UNHEALTHY),
                availability=str(ProviderAvailability.UNAVAILABLE),
                failure_state=str(failure_class),
            )
        except ValueError:
            # an unknown provider is not this function's job to invent
            return
        return

    from veya.remote.executor_health import ExecutorFailureClass, ExecutorHealthRegistry

    ExecutorHealthRegistry().record_failure(failed_executor, ExecutorFailureClass(failure_class))


async def _failover_if_provider_failure(
    mission: Any,
    result: Any,
    *,
    failed_executor: str,
    actions: list[dict[str, Any]],
    required: dict[str, Any] | None,
    attempted: set[str],
) -> Any:
    """Retask a provider-blocked iteration onto another eligible executor.

    A provider outage must not read as completion, and must not kill a mission
    that still has a lawful alternative. When the failure produced an
    irreplaceable side effect, failover is refused instead of replaying it.
    """
    if str(getattr(result, "status", "")) not in {"blocked", "failed", "partial"}:
        return result
    failure_class = _failure_class_of(result)
    if failure_class is None:
        return result

    _record_failure_against_the_responsible_layer(failed_executor, failure_class)

    # Selection reads executor health. The registry that just took the charge is
    # the same object selection consults, so a provider fault stays out of it.
    from veya.remote.executor_health import ExecutorHealthRegistry

    health = ExecutorHealthRegistry()

    if not _replay_safe(result):
        blocked = types.SimpleNamespace(
            goal_id=getattr(result, "goal_id", None),
            status="blocked",
            tasks={},
            final_summary=(
                f"executor {failed_executor!r} failed with {failure_class} after producing "
                f"side effects; failover refused because replay is not safe"
            ),
            unfinished_work=[f"provider failure after side effects: {failure_class}"],
            block_reason="FAILOVER_REPLAY_UNSAFE",
            executor_inventory=executor_inventory(local_capable=bool(actions)),
        )
        return blocked

    hint = _assignee_hint(mission)
    selected, eligible = select_mission_executor(
        requested=None if hint == failed_executor else hint,
        required_capabilities=required,
        health_registry=health,
        local_capable=bool(actions),
        allow_autonomous_external=bool(hint),
    )
    remaining = [
        candidate
        for candidate in eligible
        if candidate.executor_id not in attempted and candidate.eligible
    ]
    selected = next(
        (
            candidate
            for candidate in remaining
            if candidate.executor_id == getattr(selected, "executor_id", None)
        ),
        None,
    ) or (remaining[0] if remaining else None)
    if selected is None:
        return _no_execution_target(
            mission,
            hint=hint,
            eligible=eligible,
            has_actions=bool(actions),
            failure_class=failure_class,
        )

    retried = await _run_canonical_goal(mission, assignee=selected.executor_id, actions=actions)
    retried.executor_substitution = {
        "failed_executor": failed_executor,
        "failure_class": failure_class,
        "selected_executor": selected.executor_id,
    }
    return retried


def _no_execution_target(
    mission: Any,
    *,
    hint: str | None,
    eligible: list[ExecutorCandidate],
    has_actions: bool,
    failure_class: str | None = None,
) -> Any:
    """Fail closed and truthfully when no admitted executor can run the work."""
    if hint == _LOCAL_EXECUTOR and not has_actions:
        reason = (
            "builtin is the in-process canonical substrate: it has no external model provider "
            "and no free-text planning ability, so it can only execute planner-resolved "
            "canonical actions. This mission carries none."
        )
        unfinished = ["no planner-resolved canonical action to execute"]
    elif not has_actions:
        reason = (
            f"no healthy capable executor is available for this mission "
            f"(requested {hint!r}); every admitted executor is unreachable, "
            f"unauthenticated, or lacks the required capability"
        )
        unfinished = ["canonical executor capability is unresolved"]
    else:
        reason = (
            f"planner-resolved actions require a project file-effect executor, and no "
            f"admitted executor is healthy and capable (requested {hint!r})"
        )
        unfinished = ["canonical executor capability is unresolved"]
    if failure_class:
        reason = f"{reason} (last provider failure: {failure_class})"
    return types.SimpleNamespace(
        goal_id=None,
        status="blocked",
        tasks={},
        final_summary=reason,
        unfinished_work=unfinished,
        block_reason="NO_HEALTHY_EXECUTION_TARGET",
        executor_inventory=executor_inventory(local_capable=has_actions),
    )


async def _run_canonical_goal(mission: Any, *, assignee: str, actions: list[dict[str, Any]]) -> Any:
    """One mission iteration on the canonical GoalRun execution substrate."""
    from server.goal_run.canonical_worker import CanonicalWorkerAdapter
    from server.goal_run.runner import project_run_goal
    from server.goal_run.store import load_goal_run

    task = {
        "id": f"{mission.mission_id}-q4",
        "title": str(mission.goal)[:120],
        "instruction": str(mission.goal),
        "acceptance": list(getattr(mission, "acceptance_criteria", []) or [])
        or ["execution produced verifiable evidence"],
        "assignee": assignee,
        "depends_on": [],
    }
    # Existing typed adapter: skip only the advisory plan gate for an already
    # admitted supervision mission; execution remains the canonical leaf path.
    # ``product_canonical`` is the feature whose success evidence is "a
    # GoalRun-owned canonical action result is observed" — the truthful
    # criterion for this plane. The ``veya_cli`` default would demand CLI exit
    # codes and a CLI negative case that a file-authoring mission never produces,
    # so the verifier would hold every such mission INSUFFICIENT.
    integration_adapter = CanonicalWorkerAdapter.for_capability(
        task_id=str(task["id"]),
        objective=str(mission.goal),
        capability=None,
        feature_name="product_canonical",
        verification_required=True,
    )
    integration_adapter.skip_plan_review = True
    # A planner-resolved action is already a decision. Binding the canonical L0
    # tool dispatch lets GoalRun perform it through its ONE physical boundary
    # (ActionGateway -> PermissionEngine -> L0 tool) with no second authority and
    # no additional external model call.
    if actions:
        integration_adapter.resolved_actions = actions
        integration_adapter.gateway_executor = _local_action_dispatch(str(mission.workspace or ""))
    response = await project_run_goal(
        project_root=str(mission.workspace or ""),
        goal=str(mission.goal),
        tasks=[task],
        mode="act_eager",
        wait=True,
        verification_required=True,
        integration_adapter=integration_adapter,
    )
    state = load_goal_run(str(mission.workspace or ""), response.goal_id)
    if state is not None:
        return state
    return types.SimpleNamespace(
        goal_id=response.goal_id,
        status=str(response.status),
        tasks={},
        final_summary=response.summary
        or response.block_reason
        or "GoalRun produced no durable state",
        unfinished_work=[response.block_reason] if response.block_reason else [],
    )


def _execution_mode(mission: Any) -> str:
    """Explicit L2 execution mode (``execution_policy.mode``). No auto-guessing."""

    policies = getattr(mission, "policies", None)
    execution = getattr(policies, "execution_policy", None) or {}
    return str(execution.get("mode") or "").strip().lower()


def _local_action_dispatch(workspace: str) -> Callable[[Any], Awaitable[str]]:
    """Bind the canonical L0 tool dispatch to one mission workspace.

    This is the *HOW* half of the split. The planner already decided WHAT; this
    only invokes the same registered tool the product mainline uses, with the
    mission workspace bound as the write root. The call is still wrapped by
    GoalRun's ActionGateway, so policy, approval, audit and the side-effect
    ledger all apply exactly as they do for any other physical step.
    """
    from server import tool_registry as _tr

    async def _dispatch(request: Any) -> str:
        read_token = _tr.bind_workspace_root(workspace)
        write_token = _tr.bind_write_root(workspace)
        try:
            if not _tr.master_tools.has(request.tool):
                raise ValueError(f"unknown canonical L0 tool: {request.tool!r}")
            return str(await _tr.master_tools.execute(request.tool, dict(request.arguments)))
        finally:
            _tr.reset_write_root(write_token)
            _tr.reset_workspace_root(read_token)

    return _dispatch


def runner_with(dispatch: Dispatch) -> Callable[[Any], Awaitable[Any]]:
    async def _run(mission: Any) -> Any:
        return await canonical_runner(mission, dispatch=dispatch)

    return _run


def select_runner(
    *,
    orchestrated_dispatch: Any = None,
    orchestrated_decompose: Any = None,
) -> Callable[[Any], Awaitable[Any]]:
    """One runner selection for the ONE MissionLoop.

    ``execution_policy.mode=veya_orchestrated`` routes to the L2 orchestration
    scheduler over the L1 substrate; every other mode keeps the existing
    canonical project dispatch. No second loop, no shadow state.
    """

    async def _run(mission: Any) -> Any:
        if _execution_mode(mission) == "veya_orchestrated" and orchestrated_dispatch is not None:
            from .orchestrated import orchestrated_runner

            return await orchestrated_runner(
                mission,
                dispatch=orchestrated_dispatch,
                decompose=orchestrated_decompose,
            )
        return await canonical_runner(mission)

    return _run


__all__ = ["canonical_runner", "executor_hint", "runner_with", "select_runner"]
