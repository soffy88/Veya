"""Skill eval harness — real trigger/permission/context measurement (plane J/K).

Never marks a skill qualified because ``SKILL.md`` exists. Measures trigger
precision/recall, permission regression, latency and context cost from explicit
eval cases, including negative (must-not-trigger / permission-denied) cases.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class SkillEvalCase:
    input_task: str
    expected_trigger: bool
    expected_permission_ok: bool = True


def evaluate_skill(
    registry: Any,
    skill_id: str,
    cases: list[SkillEvalCase],
    *,
    allowed_permissions: list[str],
) -> dict[str, Any]:
    """Return precision/recall + permission regression + cost for one skill."""

    true_positive = false_positive = true_negative = false_negative = 0
    permission_regressions = 0
    latencies: list[float] = []
    context_sizes: list[int] = []
    outcomes: list[dict[str, Any]] = []

    for case in cases:
        started = time.monotonic()
        resolution = registry.resolve(case.input_task)
        triggered = skill_id in resolution["candidates"]
        latency_ms = (time.monotonic() - started) * 1000
        latencies.append(latency_ms)

        if triggered and case.expected_trigger:
            true_positive += 1
        elif triggered and not case.expected_trigger:
            false_positive += 1
        elif not triggered and case.expected_trigger:
            false_negative += 1
        else:
            true_negative += 1

        permission_ok, _missing = registry.check_permissions(skill_id, allowed_permissions)
        if case.expected_permission_ok and not permission_ok:
            permission_regressions += 1

        core_size = 0
        if triggered and permission_ok:
            core_size = len(registry.load_core(skill_id))
        context_sizes.append(core_size)
        outcomes.append(
            {
                "input_task": case.input_task,
                "expected_trigger": case.expected_trigger,
                "actual_trigger": triggered,
                "expected_permission_ok": case.expected_permission_ok,
                "actual_permission_ok": permission_ok,
                "latency_ms": round(latency_ms, 3),
                "context_size": core_size,
            }
        )

    precision = (
        true_positive / (true_positive + false_positive)
        if (true_positive + false_positive)
        else 1.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if (true_positive + false_negative)
        else 1.0
    )
    return {
        "skill_id": skill_id,
        "trigger_precision": round(precision, 3),
        "trigger_recall": round(recall, 3),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "true_negative": true_negative,
        "false_negative": false_negative,
        "permission_regression": permission_regressions == 0,
        "permission_regressions": permission_regressions,
        "latency_ms": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
        "context_cost_bytes": max(context_sizes) if context_sizes else 0,
        "cases": outcomes,
    }


__all__ = ["SkillEvalCase", "evaluate_skill"]
