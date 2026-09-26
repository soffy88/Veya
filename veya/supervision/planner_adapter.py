"""Thin adapter: Mission -> canonical planner -> validated OrchestrationPlan.

Reuses the canonical planning authority (:func:`server.goal_run.planner.g1_plan`)
for normalization/validation and the canonical Veya LLM layer
(:func:`veya.obase.llm.llm_call`) for the decomposition request. It owns no
planning state and does not reimplement decomposition — ``g1_plan`` remains the
task-graph authority; this adapter only supplies the LLM-produced
``explicit_tasks`` and maps the result into L1 subtasks.

Worker availability is passed as real evidence. If the planner suggests a
temporarily-unavailable worker (e.g. Codex), routing must re-route/replan via the
canonical policy — never silently substitute another worker.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .orchestrated import Subtask, validate_plan


@dataclass(frozen=True)
class OrchestrationPlan:
    """Canonical versioned DAG produced by the canonical planner."""

    plan_id: str
    plan_version: int
    mission_id: str
    tasks: list[Subtask]
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "mission_id": self.mission_id,
            "tasks": [t.to_dict() for t in self.tasks],
            "created_at": self.created_at,
        }


DecomposeLLM = Callable[[list[dict[str, str]]], Awaitable[str]]

_SYSTEM = (
    "You are Veya's task decomposer. Given a mission goal, produce a task graph as "
    "strict JSON only. Each task must name an assignee from the provided available "
    "workers. Do not invent workers. Use ONLY relative file paths inside the "
    "workspace (never absolute paths, never the workspace root path itself). "
    "An artifact-producing task may declare relative required_artifacts. "
    'Reply with exactly: {"subtasks":[{"id":"task-a","goal":"...",'
    '"worker":"hicode","dependencies":[],"required_artifacts":[]}]}'
)


def _prompt(goal: str, workspace: str, available: list[str], unavailable: dict[str, str]) -> str:
    return json.dumps(
        {
            "goal": goal,
            "workspace": workspace,
            "available_workers": available,
            "temporarily_unavailable_workers": unavailable,
            "constraints": [
                "Use at least 3 independent first-wave subtasks when the goal allows.",
                "Use a dependent second-wave subtask where a real dependency exists.",
                "Only use workers from available_workers.",
                "Use relative file paths only; never absolute paths.",
            ],
        },
        ensure_ascii=False,
    )


async def _canonical_llm(messages: list[dict[str, str]]) -> str:
    """Canonical Veya LLM request (Veya gateway), with the obase llm_call facade.

    The deployment's canonical provider is the local Veya LLM gateway (the same
    one Hicode uses). ``veya.obase.llm.llm_call`` is tried first; when it is not
    configured (shim response) the gateway is called directly.
    """

    try:
        from veya.obase.llm import llm_call

        response = await llm_call(messages)
        content = str(
            ((response.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        )
        usage = response.get("usage") or {}
        if content and "shim response" not in content and usage.get("total_tokens"):
            return content
    except Exception:
        pass
    return await gateway_llm(messages)


async def gateway_llm(
    messages: list[dict[str, str]], *, response_format: dict[str, Any] | None = None
) -> str:
    """Canonical Veya gateway chat completion (real provider, no shim)."""

    import os

    import httpx

    base = os.environ.get("HICODE_REASONIX_BASE_URL") or "http://127.0.0.1:8791/v1"
    model = (
        os.environ.get("HICODE_REASONIX_MODEL")
        or os.environ.get("VEYA_LLM_MODEL")
        # SPEC 3: the planner task class routes to the canonical master brain
        or "veya1.2"
    )
    key_env = os.environ.get("HICODE_REASONIX_API_KEY_ENV") or "OPENCODE_API_KEY"
    key = os.environ.get(key_env) or ""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0}
        if response_format is not None:
            body["response_format"] = response_format
        response = await client.post(
            f"{base.rstrip('/')}/chat/completions",
            headers=headers,
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
    choices = payload.get("choices") or []
    if not choices:
        raise ValueError("gateway LLM returned no choices")
    return str((choices[0].get("message") or {}).get("content") or "")


def _extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("planner output is not JSON")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("planner output must be a JSON object")
    return parsed


async def decompose(
    mission: Any,
    *,
    workspace: str,
    available_workers: list[str],
    temporarily_unavailable_workers: dict[str, str] | None = None,
    health_registry: Any = None,
    llm: DecomposeLLM | None = None,
) -> list[Subtask]:
    """Mission -> canonical planner -> validated list of L1 subtasks."""

    unavailable = dict(temporarily_unavailable_workers or {})
    avail = list(available_workers)

    if health_registry is not None:
        from veya.remote.models import ExecutorHealth

        filtered_avail = []
        for w in avail:
            worker_key = w.strip().lower()
            health = health_registry.get_health(worker_key)
            if health in (ExecutorHealth.UNAVAILABLE, ExecutorHealth.DEGRADED):
                rec = getattr(health_registry, "_records", {}).get(worker_key)
                reason = (
                    getattr(rec, "last_detail", "") if rec else ""
                ) or f"HEALTH_{getattr(health, 'value', health)}"
                unavailable[worker_key] = reason
            else:
                filtered_avail.append(w)
        avail = filtered_avail

    goal = str(getattr(mission, "goal", "") or "")
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _prompt(goal, workspace, avail, unavailable)},
    ]
    raw = await (llm or _canonical_llm)(messages)
    plan = _extract_json(raw)
    explicit = plan.get("subtasks") or []
    if not isinstance(explicit, list) or not explicit:
        raise ValueError("planner produced no subtasks")

    # Canonical authority: g1_plan normalizes the explicit task graph.
    from server.goal_run.planner import g1_plan

    state, _response = await g1_plan(
        interpretation=goal,
        assumptions=[],
        goal_text=goal,
        default_assignee=avail[0].lower() if avail else "hicode",
        explicit_tasks=[
            {
                "id": str(item.get("id")),
                "title": str(item.get("goal") or item.get("title") or ""),
                "instruction": str(item.get("goal") or item.get("instruction") or ""),
                "depends_on": [
                    str(d) for d in item.get("dependencies") or item.get("depends_on") or []
                ],
                "assignee": str(item.get("worker") or item.get("assignee") or "").lower(),
                "acceptance": [str(item.get("goal") or "")],
            }
            for item in explicit
        ],
    )
    declared_artifacts = {
        str(item.get("id")): tuple(str(path) for path in (item.get("required_artifacts") or []))
        for item in explicit
        if isinstance(item, dict)
    }
    subtasks = [
        Subtask(
            task_id=str(node.id),
            objective=str(node.instruction or node.title),
            worker=str(node.assignee).lower(),
            depends_on=tuple(str(d) for d in (node.depends_on or [])),
            required_artifacts=declared_artifacts.get(str(node.id), ()),
        )
        for node in state.tasks.values()
    ]
    validate_plan(subtasks)
    allowed = {str(w).strip().lower() for w in avail}
    for subtask in subtasks:
        if subtask.worker not in allowed:
            # Unknown / temporarily-unavailable worker: block and require
            # re-route/replan through the canonical policy — never substitute.
            from .orchestrated import OrchestrationError

            raise OrchestrationError(
                f"planner selected unavailable worker {subtask.worker!r}; "
                "re-route/replan required (no cross-worker substitution)"
            )
    return subtasks


async def decompose_plan(
    mission: Any,
    *,
    workspace: str,
    available_workers: list[str],
    temporarily_unavailable_workers: dict[str, str] | None = None,
    health_registry: Any = None,
    llm: DecomposeLLM | None = None,
    plan_version: int = 1,
) -> OrchestrationPlan:
    """Mission -> canonical planner -> validated OrchestrationPlan."""
    tasks = await decompose(
        mission,
        workspace=workspace,
        available_workers=available_workers,
        temporarily_unavailable_workers=temporarily_unavailable_workers,
        health_registry=health_registry,
        llm=llm,
    )
    mission_id = str(getattr(mission, "mission_id", "") or "mission")
    plan_id = f"plan-{mission_id}-v{plan_version}"
    return OrchestrationPlan(
        plan_id=plan_id,
        plan_version=plan_version,
        mission_id=mission_id,
        tasks=tasks,
    )


def planner_decompose(
    *,
    available_workers: list[str],
    temporarily_unavailable_workers: dict[str, str] | None = None,
    health_registry: Any = None,
    llm: DecomposeLLM | None = None,
) -> Callable[[Any], Awaitable[list[Subtask]]]:
    """Bind availability/LLM into the ``orchestrated_runner`` decompose callable."""

    async def _decompose(mission: Any) -> list[Subtask]:
        workspace = str(getattr(mission, "workspace", "") or "")
        return await decompose(
            mission,
            workspace=workspace,
            available_workers=available_workers,
            temporarily_unavailable_workers=temporarily_unavailable_workers,
            health_registry=health_registry,
            llm=llm,
        )

    return _decompose


__all__ = [
    "OrchestrationPlan",
    "decompose",
    "decompose_plan",
    "gateway_llm",
    "planner_decompose",
]
