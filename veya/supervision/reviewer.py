"""InternalSupervisor — design and *independent* review (spec §9/§10).

Two hard rules:

* The internal supervisor never performs a side effect itself: it designs,
  plans, reviews and retasks; execution goes through Planner → Runtime →
  Hicode/DSH/worker.
* Review runs in a fresh context containing only the mission, its acceptance
  criteria, the ExecutionReport and the evidence. It must not inherit the
  executor's chain, its self-assessment, or planner hidden assumptions —
  so ``self-design ≠ self-certification``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .models import ExecutionReport, Mission, ReviewDecision, SupervisorReview

ReviewLLM = Callable[[str], Awaitable[str]]
_DECISIONS = {str(d) for d in ReviewDecision}


class SupervisorUnavailable(RuntimeError):
    """No reviewer model is configured; the caller must escalate or wait."""


@dataclass
class ReviewContext:
    """The ONLY inputs a reviewer may see (spec §10)."""

    mission_id: str
    goal: str
    acceptance_criteria: list[str]
    constraints: list[str]
    report: dict[str, Any]
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "goal": self.goal,
            "acceptance_criteria": list(self.acceptance_criteria),
            "constraints": list(self.constraints),
            "report": self.report,
            "evidence": list(self.evidence),
        }


class InternalSupervisor:
    name = "internal"

    def __init__(self, llm: ReviewLLM | None = None) -> None:
        self._llm = llm

    # ── review ──────────────────────────────────────────────────────
    def build_review_context(self, mission: Mission, report: ExecutionReport) -> ReviewContext:
        return ReviewContext(
            mission_id=mission.mission_id,
            goal=mission.goal,
            acceptance_criteria=list(mission.acceptance_criteria),
            constraints=list(mission.constraints),
            report=report.to_dict(),
            evidence=list(report.runtime_evidence) + list(report.artifacts) + list(report.tests),
        )

    async def review(self, mission: Mission, report: ExecutionReport) -> SupervisorReview:
        if self._llm is None:
            raise SupervisorUnavailable("internal supervisor has no reviewer model")
        context = self.build_review_context(mission, report)
        raw = await self._llm(_review_prompt(context))
        return _parse_review(mission, report, raw, supervisor=self.name)

    # ── design ──────────────────────────────────────────────────────
    async def design(self, mission: Mission) -> dict[str, Any]:
        if self._llm is None:
            raise SupervisorUnavailable("internal supervisor has no design model")
        raw = await self._llm(_design_prompt(mission))
        parsed = _extract_json(raw)
        if not isinstance(parsed, dict):
            raise ValueError("design did not return a JSON object")
        return parsed


def _review_prompt(context: ReviewContext) -> str:
    return (
        "You are an independent reviewer. Judge ONLY from the provided mission, "
        "acceptance criteria, execution report and evidence. Do not assume the "
        "executor's summary is correct. Reply with a single JSON object and nothing "
        "else, using exactly these keys: decision (one of "
        f"{sorted(_DECISIONS)}), reason, next_task, constraints_delta, "
        "acceptance_delta, required_evidence, risk_notes, confidence.\n"
        "Evidence and plan:\n" + json.dumps(context.payload(), ensure_ascii=False)
    )


def _design_prompt(mission: Mission) -> str:
    return (
        "Design a plan for this mission. Reply with a single JSON object holding "
        "keys: tasks (list of {objective, acceptance, executor, side_effect_class}), "
        "risks, and rationale. Executors are one of hicode, dsh, worker, native_tool.\n"
        "Mission:\n" + json.dumps(mission.to_dict(), ensure_ascii=False)
    )


def _extract_json(raw: str) -> Any:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _parse_review(
    mission: Mission,
    report: ExecutionReport,
    raw: str,
    *,
    supervisor: str,
) -> SupervisorReview:
    parsed = _extract_json(raw)
    if not isinstance(parsed, dict) or str(parsed.get("decision", "")) not in _DECISIONS:
        # A reviewer that cannot produce a valid verdict is a signal to escalate,
        # never a silent ACCEPT.
        return SupervisorReview(
            mission_id=mission.mission_id,
            iteration=report.iteration,
            supervisor=supervisor,
            decision=ReviewDecision.escalate,
            reason="reviewer output was not a valid SupervisorReview",
            risk_notes=["unparseable reviewer output"],
        )
    return SupervisorReview(
        mission_id=mission.mission_id,
        iteration=report.iteration,
        supervisor=supervisor,
        decision=ReviewDecision(str(parsed["decision"])),
        reason=str(parsed.get("reason", "")),
        next_task=parsed.get("next_task"),
        constraints_delta=[str(x) for x in parsed.get("constraints_delta") or []],
        acceptance_delta=[str(x) for x in parsed.get("acceptance_delta") or []],
        required_evidence=[str(x) for x in parsed.get("required_evidence") or []],
        risk_notes=[str(x) for x in parsed.get("risk_notes") or []],
        confidence=parsed.get("confidence"),
    )


__all__ = ["InternalSupervisor", "ReviewContext", "SupervisorUnavailable"]
