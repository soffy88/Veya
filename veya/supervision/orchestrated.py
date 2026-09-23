"""L2 orchestration: dependency-aware scheduling over the L1 worker substrate.

This module does **not** create a second Mission / Execution / Evidence / Review
runtime. It produces the canonical :class:`~veya.supervision.models.ExecutionReport`
that the existing supervision loop consumes, and persists routing decisions as
evidence entries. Dispatch is injected (the L1 ``worker.dispatch`` substrate), so
there is exactly one execution engine (``DUPLICATE_EXECUTION_ENGINE=NO``).

Boundary (L1 vs L2): this layer only *schedules already-decomposed subtasks onto
named workers*. It does not invent a plan from free text, does not rank results,
and does not decide acceptance — review/acceptance stay in the supervision loop.
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import ExecutionReport

# L1 canonical executors.
L1_WORKERS: tuple[str, ...] = ("hicode", "dsh", "pi", "grok", "codex")

_COMPLETED = "COMPLETED"
_BLOCKED_BY_DEPENDENCY = "BLOCKED_BY_DEPENDENCY"


class OrchestrationError(ValueError):
    """Invalid plan (unknown worker, unknown dependency, cycle)."""


def _relative_artifacts(raw: Any) -> tuple[str, ...]:
    """Normalize declared deliverables without accepting workspace escapes."""

    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise OrchestrationError("required_artifacts must be a list of relative paths")
    paths: list[str] = []
    for value in raw:
        path = Path(str(value).strip())
        if not str(path) or path == Path(".") or path.is_absolute() or ".." in path.parts:
            raise OrchestrationError("required_artifacts must contain relative non-escaping paths")
        normalized = path.as_posix()
        if normalized not in paths:
            paths.append(normalized)
    return tuple(paths)


@dataclass(frozen=True)
class Subtask:
    task_id: str
    objective: str
    worker: str
    depends_on: tuple[str, ...] = ()
    timeout_s: float | None = None
    acceptance: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)
    # Explicit filesystem deliverables required by downstream dependencies.
    # Empty means this task has no artifact requirement; it does not permit a
    # dependent DSH task to read another worktree.
    required_artifacts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "worker": self.worker,
            "depends_on": list(self.depends_on),
            "timeout_s": self.timeout_s,
            "acceptance": list(self.acceptance),
            "inputs": dict(self.inputs),
            "required_artifacts": list(self.required_artifacts),
        }


@dataclass
class SubtaskResult:
    task_id: str
    worker: str
    status: str
    execution_id: str | None = None
    parent_execution_id: str | None = None
    summary: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: float | None = None
    failure_class: str | None = None
    failure_source: str | None = None
    failure_message: str | None = None
    failure_detail: str | None = None
    provider_error_code: str | None = None
    exit_code: int | None = None
    phase: str | None = None
    last_event: dict[str, Any] | None = None
    model_request_count: int = 0
    tool_call_count: int = 0
    blocked_by_subtask: str | None = None
    blocked_by_execution_id: str | None = None
    artifact_requirement: str = "OPTIONAL"
    dependency_ready: bool | None = None
    required_artifacts: list[str] = field(default_factory=list)
    missing_required_artifacts: list[str] = field(default_factory=list)


Dispatch = Callable[[Subtask], Awaitable[SubtaskResult]]


def parse_subtasks(raw: Any) -> list[Subtask]:
    """Parse an explicit decomposition (``execution_policy.subtasks``).

    Decomposition itself is a planner concern; this only accepts a provided plan.
    """
    if not isinstance(raw, list):
        raise OrchestrationError("subtasks must be a list")
    out: list[Subtask] = []
    for item in raw:
        if not isinstance(item, dict):
            raise OrchestrationError("each subtask must be an object")
        out.append(
            Subtask(
                task_id=str(item["task_id"]),
                objective=str(item["objective"]),
                worker=str(item["worker"]).strip().lower(),
                depends_on=tuple(str(d) for d in item.get("depends_on") or ()),
                timeout_s=item.get("timeout_s"),
                acceptance=tuple(str(a) for a in item.get("acceptance") or ()),
                inputs=dict(item.get("inputs") or {}),
                required_artifacts=_relative_artifacts(item.get("required_artifacts") or ()),
            )
        )
    return out


def validate_plan(subtasks: list[Subtask]) -> None:
    ids = [s.task_id for s in subtasks]
    if len(set(ids)) != len(ids):
        raise OrchestrationError("duplicate task_id in plan")
    known = set(ids)
    for subtask in subtasks:
        if subtask.worker not in L1_WORKERS:
            raise OrchestrationError(
                f"unknown worker {subtask.worker!r}; expected one of {list(L1_WORKERS)}"
            )
        for dep in subtask.depends_on:
            if dep not in known:
                raise OrchestrationError(f"unknown dependency {dep!r} on {subtask.task_id}")
            if dep == subtask.task_id:
                raise OrchestrationError(f"self dependency on {subtask.task_id}")
        _relative_artifacts(subtask.required_artifacts)
    # Kahn cycle detection.
    indegree = {s.task_id: len(s.depends_on) for s in subtasks}
    children: dict[str, list[str]] = {s.task_id: [] for s in subtasks}
    for subtask in subtasks:
        for dep in subtask.depends_on:
            children[dep].append(subtask.task_id)
    queue = [tid for tid, degree in indegree.items() if degree == 0]
    seen = 0
    while queue:
        tid = queue.pop()
        seen += 1
        for child in children[tid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if seen != len(subtasks):
        raise OrchestrationError("dependency cycle detected")


def routing_evidence(subtask: Subtask, *, parent_execution_id: str, reason: str) -> dict[str, Any]:
    return {
        "kind": "routing_decision",
        "subtask_id": subtask.task_id,
        "selected_worker": subtask.worker,
        "routing_reason": reason,
        "parent_execution_id": parent_execution_id,
    }


async def run_plan(
    subtasks: list[Subtask],
    dispatch: Dispatch,
    *,
    mission_id: str,
    iteration: int,
    objective: str,
    parent_execution_id: str = "",
    failure_mode: str = "collect_all",
) -> ExecutionReport:
    """Schedule a dependency graph over L1 workers and build an ExecutionReport.

    Independent ready nodes run concurrently; a node whose dependency did not
    complete is ``BLOCKED`` and never early-started. One child failing never
    cancels its siblings (``collect_all``).
    """
    validate_plan(subtasks)
    results: dict[str, SubtaskResult] = {}
    evidence: list[dict[str, Any]] = []
    pending: dict[str, Subtask] = {s.task_id: s for s in subtasks}

    while pending:
        ready = [s for s in pending.values() if all(d in results for d in s.depends_on)]
        if not ready:
            break
        dispatchable: list[Subtask] = []
        for subtask in ready:
            failed_dep = next(
                (d for d in subtask.depends_on if results[d].status != _COMPLETED), None
            )
            if failed_dep is not None:
                dependency_result = results[failed_dep]
                dependency_message = f"dependency {failed_dep} did not complete"
                results[subtask.task_id] = SubtaskResult(
                    task_id=subtask.task_id,
                    worker=subtask.worker,
                    status=_BLOCKED_BY_DEPENDENCY,
                    error=dependency_message,
                    failure_class="DEPENDENCY_BLOCKED",
                    failure_source="l2_dependency_gate",
                    failure_message=dependency_message,
                    failure_detail=(
                        f"blocked_by_subtask={failed_dep}; "
                        f"blocked_by_execution_id={dependency_result.execution_id or 'unknown'}"
                    ),
                    blocked_by_subtask=failed_dep,
                    blocked_by_execution_id=dependency_result.execution_id,
                )
                evidence.append(
                    routing_evidence(
                        subtask,
                        parent_execution_id=parent_execution_id,
                        reason=f"blocked_by_dependency: {failed_dep} did not complete",
                    )
                )
                del pending[subtask.task_id]
            else:
                if subtask.depends_on:
                    dependency_artifacts: list[dict[str, Any]] = []
                    missing_dependency_artifacts: list[str] = []
                    for dependency_id in subtask.depends_on:
                        dependency = results[dependency_id]
                        dependency_manifest = next(
                            (
                                item
                                for item in dependency.evidence
                                if isinstance(item, dict)
                                and item.get("kind") == "artifact_manifest"
                            ),
                            None,
                        )
                        dependency_has_artifact = False
                        for item in dependency.evidence:
                            if not isinstance(item, dict) or item.get("kind") != "artifact":
                                continue
                            if not item.get("materialized_path"):
                                continue
                            dependency_has_artifact = True
                            dependency_artifacts.append(
                                {
                                    "dependency_subtask_id": dependency_id,
                                    "execution_id": dependency.execution_id,
                                    "source_worker": dependency.worker,
                                    "source_path": (
                                        str(
                                            Path(str(item.get("source_worktree") or ""))
                                            / str(item.get("relative_path") or "")
                                        )
                                    ),
                                    "relative_path": item.get("relative_path"),
                                    "materialized_path": item.get("materialized_path"),
                                    "hash": item.get("hash"),
                                    "size": item.get("size"),
                                }
                            )
                        dependency_not_ready = dependency.dependency_ready is False
                        if subtask.worker == "dsh" and (
                            dependency_manifest is None
                            or not dependency_has_artifact
                            or dependency_not_ready
                        ):
                            missing_dependency_artifacts.append(dependency_id)
                    if subtask.worker == "dsh" and missing_dependency_artifacts:
                        dependency_message = (
                            "dependency artifact staging failed before DSH dispatch; "
                            f"missing artifacts from {', '.join(missing_dependency_artifacts)}"
                        )
                        results[subtask.task_id] = SubtaskResult(
                            task_id=subtask.task_id,
                            worker=subtask.worker,
                            status="BLOCKED",
                            error=dependency_message,
                            failure_class="DEPENDENCY_ARTIFACT_STAGING_FAILED",
                            failure_source="l2_dependency_artifact_gate",
                            failure_message=dependency_message,
                            failure_detail=(
                                "DSH_DISPATCHED=NO; "
                                f"missing_dependency_subtasks={missing_dependency_artifacts}"
                            ),
                            blocked_by_subtask=missing_dependency_artifacts[0],
                            blocked_by_execution_id=results[
                                missing_dependency_artifacts[0]
                            ].execution_id,
                        )
                        evidence.append(
                            routing_evidence(
                                subtask,
                                parent_execution_id=parent_execution_id,
                                reason=dependency_message,
                            )
                        )
                        del pending[subtask.task_id]
                        continue
                    if dependency_artifacts:
                        from dataclasses import replace

                        subtask = replace(
                            subtask,
                            inputs={
                                **subtask.inputs,
                                "dependency_artifacts": dependency_artifacts,
                            },
                        )
                dispatchable.append(subtask)
        if not dispatchable:
            continue
        for subtask in dispatchable:
            evidence.append(
                routing_evidence(
                    subtask,
                    parent_execution_id=parent_execution_id,
                    reason="explicit subtask.worker (no auto routing)",
                )
            )
        outcomes = await asyncio.gather(*(dispatch(s) for s in dispatchable))
        for subtask, result in zip(dispatchable, outcomes, strict=True):
            results[subtask.task_id] = result
            del pending[subtask.task_id]

    ordered = [results[s.task_id] for s in subtasks]
    completed = [r for r in ordered if r.status == _COMPLETED]
    failed = [r for r in ordered if r.status == "FAILED"]
    blocked = [r for r in ordered if r.status in {"BLOCKED", _BLOCKED_BY_DEPENDENCY}]
    cancelled = [r for r in ordered if r.status == "CANCELLED"]

    if not ordered:
        status = "failed"
    elif len(completed) == len(ordered):
        status = "completed"
    elif completed:
        status = "partial"
    elif cancelled and len(cancelled) == len(ordered):
        status = "cancelled"
    else:
        status = "failed"

    changes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    blocked_items: list[dict[str, Any]] = []
    runtime_evidence: list[dict[str, Any]] = list(evidence)
    artifacts: list[dict[str, Any]] = []
    by_path: dict[str, list[dict[str, Any]]] = {}
    for result in ordered:
        runtime_evidence.append(
            {
                "kind": "l1_execution",
                "subtask_id": result.task_id,
                "worker": result.worker,
                "status": result.status,
                "execution_id": result.execution_id,
                "parent_execution_id": result.parent_execution_id,
                "elapsed_ms": result.elapsed_ms,
                "failure_class": result.failure_class,
                "failure_source": result.failure_source,
                "failure_message": result.failure_message or result.error,
                "failure_detail": result.failure_detail,
                "provider_error_code": result.provider_error_code,
                "exit_code": result.exit_code,
                "phase": result.phase,
                "last_event": result.last_event,
                "model_request_count": result.model_request_count,
                "tool_call_count": result.tool_call_count,
                "blocked_by_subtask": result.blocked_by_subtask,
                "blocked_by_execution_id": result.blocked_by_execution_id,
                "artifact_requirement": result.artifact_requirement,
                "dependency_ready": result.dependency_ready,
                "required_artifacts": list(result.required_artifacts),
                "missing_required_artifacts": list(result.missing_required_artifacts),
            }
        )
        changes.extend(result.evidence)
        for item in result.evidence:
            if isinstance(item, dict) and item.get("kind") == "artifact":
                artifacts.append(item)
                by_path.setdefault(str(item.get("relative_path")), []).append(item)
        if result.status == "FAILED":
            failures.append(
                {
                    "task_id": result.task_id,
                    "worker": result.worker,
                    "error": result.error,
                    "failure_class": result.failure_class,
                    "failure_source": result.failure_source,
                    "failure_message": result.failure_message or result.error,
                    "failure_detail": result.failure_detail,
                    "provider_error_code": result.provider_error_code,
                    "exit_code": result.exit_code,
                    "phase": result.phase,
                    "last_event": result.last_event,
                }
            )
        if result.status in {"BLOCKED", _BLOCKED_BY_DEPENDENCY}:
            blocked_items.append(
                {
                    "task_id": result.task_id,
                    "worker": result.worker,
                    "status": result.status,
                    "error": result.error,
                    "failure_class": result.failure_class,
                    "failure_source": result.failure_source,
                    "failure_message": result.failure_message or result.error,
                    "failure_detail": result.failure_detail,
                    "provider_error_code": result.provider_error_code,
                    "exit_code": result.exit_code,
                    "phase": result.phase,
                    "last_event": result.last_event,
                    "blocked_by_subtask": result.blocked_by_subtask,
                    "blocked_by_execution_id": result.blocked_by_execution_id,
                    "artifact_requirement": result.artifact_requirement,
                    "dependency_ready": result.dependency_ready,
                    "required_artifacts": list(result.required_artifacts),
                    "missing_required_artifacts": list(result.missing_required_artifacts),
                }
            )
    # Collision policy: two children writing the same relative path is never
    # last-write-wins. Both sources are preserved and flagged for Review/Veya.
    for path, items in by_path.items():
        if len({str(item.get("execution_id")) for item in items}) > 1:
            blocked_items.append(
                {
                    "kind": "ARTIFACT_COLLISION",
                    "relative_path": path,
                    "execution_ids": [item.get("execution_id") for item in items],
                }
            )
            runtime_evidence.append(
                {"kind": "ARTIFACT_COLLISION", "relative_path": path, "sources": items}
            )

    report = ExecutionReport(
        mission_id=mission_id,
        iteration=iteration,
        objective=objective,
        status=status,
        changes=changes,
        artifacts=artifacts,
        runtime_evidence=runtime_evidence,
        failures=failures,
        blocked_items=blocked_items,
        executor_summary=(
            f"L2 orchestration: {len(completed)}/{len(ordered)} subtasks completed "
            f"({len(failed)} failed, {len(blocked)} blocked)"
        ),
        proposed_next_action="retask" if (failed or blocked) else None,
    )
    return report


# ── runner wiring (one MissionLoop, one report path) ───────────────────
Decompose = Callable[[Any], Awaitable[list[Subtask]]]

_WORKER_TO_NODE_STATUS = {
    "COMPLETED": "completed",
    "FAILED": "failed",
    "BLOCKED": "blocked",
    "BLOCKED_BY_DEPENDENCY": "blocked",
    "CANCELLED": "cancelled",
}


def report_to_state(report: ExecutionReport) -> Any:
    """Project the orchestration report onto the canonical goalrun-state shape.

    ``evidence.build_execution_report`` consumes this, so there is exactly one
    Evidence runtime and one report type.
    """

    routing: dict[str, list[dict[str, Any]]] = {}
    executions: dict[str, dict[str, Any]] = {}
    for entry in report.runtime_evidence:
        if entry.get("kind") == "routing_decision":
            routing.setdefault(str(entry.get("subtask_id")), []).append(entry)
        elif entry.get("kind") == "l1_execution":
            executions[str(entry.get("subtask_id"))] = entry
    tasks: dict[str, Any] = {}
    artifacts_by_task: dict[str, list[dict[str, Any]]] = {}
    for item in report.artifacts:
        artifacts_by_task.setdefault(str(item.get("subtask_id")), []).append(item)
    for task_id, entry in executions.items():
        manifest = artifacts_by_task.get(task_id, [])
        paths = [
            str(
                item.get("materialized_path")
                or str(
                    Path(str(item.get("source_worktree") or "")) / str(item.get("relative_path"))
                )
            )
            for item in manifest
        ]
        tasks[task_id] = types.SimpleNamespace(
            title=task_id,
            assignee=entry.get("worker", ""),
            status=_WORKER_TO_NODE_STATUS.get(str(entry.get("status")), "failed"),
            evidence=[*routing.get(task_id, []), entry, *manifest],
            acceptance=[],
            artifacts=paths,
            block_reason=(
                entry.get("failure_message") or entry.get("failure_detail") or entry.get("error")
            ),
            retries=0,
            unfinished_work=None,
        )
    status = {
        "completed": "executed",
        "partial": "partial",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(report.status, "failed")
    return types.SimpleNamespace(
        status=status,
        tasks=tasks,
        final_summary=report.executor_summary,
        unfinished_work=[str(f.get("task_id")) for f in report.failures],
    )


def plan_from_mission(mission: Any) -> list[Subtask]:
    """Explicit plan from ``execution_policy.subtasks`` (if provided)."""

    policies = getattr(mission, "policies", None)
    execution = getattr(policies, "execution_policy", None) or {}
    raw = execution.get("subtasks")
    if not raw:
        return []
    return parse_subtasks(raw)


async def orchestrated_runner(
    mission: Any,
    *,
    dispatch: Dispatch,
    decompose: Decompose | None = None,
) -> Any:
    """MissionLoop runner for ``execution_policy.mode=veya_orchestrated``.

    Decomposition is explicit (``subtasks``) or injected (``decompose``, an LLM
    planner). This runner only schedules and collects; review/acceptance stay in
    the supervision loop.
    """

    subtasks = plan_from_mission(mission)
    if not subtasks and decompose is not None:
        subtasks = await decompose(mission)
    if not subtasks:
        subtasks = [Subtask(task_id="task-1", objective=str(mission.goal), worker="hicode")]
    validate_plan(subtasks)
    authority = getattr(mission, "authority", None) or {}
    report = await run_plan(
        subtasks,
        dispatch,
        mission_id=str(mission.mission_id),
        iteration=int(authority.get("iteration") or 0),
        objective=str(mission.goal),
        parent_execution_id=str(authority.get("execution_id") or ""),
    )
    return report_to_state(report)


__all__ = [
    "L1_WORKERS",
    "OrchestrationError",
    "Subtask",
    "SubtaskResult",
    "orchestrated_runner",
    "parse_subtasks",
    "plan_from_mission",
    "report_to_state",
    "routing_evidence",
    "run_plan",
    "validate_plan",
]
