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

from .models import CORRECTION_SCOPES, ExecutionReport, Mission, ReviewDecision, SupervisorReview

ReviewLLM = Callable[[str], Awaitable[str]]
_DECISIONS = {str(d) for d in ReviewDecision}
_DECISION_BY_LOWER = {d.lower(): d for d in _DECISIONS}
_RAW_REVIEW_KEYS = frozenset(
    {"stdout", "stderr", "prompt", "raw_prompt", "worker_prompt", "event_stream", "events"}
)
_REPORT_KEYS = frozenset(
    {
        "mission_id",
        "iteration",
        "objective",
        "status",
        "changes",
        "tests",
        "artifacts",
        "runtime_evidence",
        "git_diff_summary",
        "failures",
        "unresolved_risks",
        "deviations",
        "blocked_items",
        "jev_decisions",
        "executor_summary",
        "proposed_next_action",
        "plan",
        "plan_version",
    }
)


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


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    """Keep review input evidence-shaped; raw logs/prompts never cross this boundary."""

    if depth > 3:
        return str(value)[:400]
    if isinstance(value, dict):
        return {
            str(key): _bounded_value(item, depth=depth + 1)
            for key, item in value.items()
            if str(key).lower() not in _RAW_REVIEW_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_bounded_value(item, depth=depth + 1) for item in list(value)[:50]]
    if isinstance(value, str):
        return value[:800]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:400]


def _bounded_report(report: dict[str, Any], mission: Mission) -> dict[str, Any]:
    """Project the canonical report into the bounded reviewer contract."""

    bounded = {
        str(key): _bounded_value(value) for key, value in report.items() if str(key) in _REPORT_KEYS
    }
    plan = mission.authority.get("plan")
    if not isinstance(plan, list):
        plan = (mission.policies.execution_policy or {}).get("subtasks") or []
    bounded["plan"] = _bounded_value(plan)
    bounded["plan_version"] = int(mission.authority.get("plan_version", 1))
    return bounded


class InternalSupervisor:
    name = "internal"

    def __init__(self, llm: ReviewLLM | None = None) -> None:
        self._llm = llm

    # ── review ──────────────────────────────────────────────────────
    def build_review_context(self, mission: Mission, report: ExecutionReport) -> ReviewContext:
        bounded = _bounded_report(report.to_dict(), mission)
        return ReviewContext(
            mission_id=mission.mission_id,
            goal=mission.goal,
            acceptance_criteria=list(mission.acceptance_criteria),
            constraints=list(mission.constraints),
            report=bounded,
            evidence=(
                list(bounded.get("runtime_evidence") or [])
                + list(bounded.get("artifacts") or [])
                + list(bounded.get("tests") or [])
            ),
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
        f"{sorted(_DECISIONS)}), correction_scope (one of {sorted(CORRECTION_SCOPES)}), "
        "reason, next_task, constraints_delta, "
        "acceptance_delta, required_evidence, risk_notes, confidence.\n"
        "Use decision REVISE with correction_scope PLAN for plan-level correction; "
        "there is no separate REPLAN decision.\n"
        "Evidence and plan:\n" + json.dumps(context.payload(), ensure_ascii=False)
    )


def _design_prompt(mission: Mission) -> str:
    return (
        "Design a plan for this mission. Reply with a single JSON object holding "
        "keys: tasks (list of {objective, acceptance, executor, side_effect_class}), "
        "risks, and rationale. Executors are one of dsh, builtin.\n"
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
    if not isinstance(parsed, dict):
        parsed = {}
    decision = _DECISION_BY_LOWER.get(str(parsed.get("decision", "")).strip().lower())
    scope = str(parsed.get("correction_scope", "TASK")).strip().upper()
    if decision is None or scope not in CORRECTION_SCOPES:
        # A reviewer that cannot produce a valid verdict is a signal to escalate,
        # never a silent ACCEPT.
        review = SupervisorReview(
            mission_id=mission.mission_id,
            iteration=report.iteration,
            supervisor=supervisor,
            decision=ReviewDecision.escalate,
            reason="reviewer output was not a valid SupervisorReview",
            risk_notes=["unparseable reviewer output"],
        )
        review.raw_review = {"raw_text": str(raw)[:20_000], "parsed": parsed}
        return review
    review = SupervisorReview(
        mission_id=mission.mission_id,
        iteration=report.iteration,
        supervisor=supervisor,
        decision=ReviewDecision(decision),
        correction_scope=scope,
        reason=str(parsed.get("reason", "")),
        next_task=parsed.get("next_task"),
        constraints_delta=[str(x) for x in parsed.get("constraints_delta") or []],
        acceptance_delta=[str(x) for x in parsed.get("acceptance_delta") or []],
        required_evidence=[str(x) for x in parsed.get("required_evidence") or []],
        risk_notes=[str(x) for x in parsed.get("risk_notes") or []],
        confidence=parsed.get("confidence"),
    )
    review.raw_review = {"raw_text": str(raw)[:20_000], "parsed": parsed}
    return review


def _valid_review(raw: str) -> dict[str, Any] | None:
    """Strict SupervisorReview validation: JSON object + canonical decision."""

    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        return None
    canonical = _DECISION_BY_LOWER.get(str(parsed.get("decision", "")).strip().lower())
    if canonical is None:
        return None
    scope = str(parsed.get("correction_scope", "TASK")).strip().upper()
    if scope not in CORRECTION_SCOPES:
        return None
    parsed["decision"] = canonical
    parsed["correction_scope"] = scope
    return parsed


_REPAIR_SUFFIX = (
    "\n\nYour previous response did not conform to SupervisorReview. "
    "Return ONLY one valid JSON object matching this schema and nothing else: "
    '{"decision":"ACCEPT|CONTINUE|REVISE|RETRY|ROLLBACK|ESCALATE|DONE",'
    '"correction_scope":"TASK|PLAN","reason":"string",'
    '"next_task":null,"constraints_delta":[],"acceptance_delta":[],'
    '"required_evidence":[],"risk_notes":[],"confidence":null}'
)


def strict_reviewer_llm(
    base: Callable[[str], Awaitable[str]], *, recorder: dict[str, Any] | None = None
) -> Callable[[str], Awaitable[str]]:
    """Strict structured-output wrapper for a reviewer LLM.

    Parse -> validate against the canonical ``SupervisorReview`` decision set ->
    at most ONE repair request. A second failure is fail-closed (ESCALATE), never
    an infinite retry and never a decision guessed from prose.
    """

    state = recorder if recorder is not None else {}
    state.setdefault("request_count", 0)
    state.setdefault("repair_retry", False)
    state.setdefault("parse_attempt_1", None)
    state.setdefault("parse_attempt_2", None)

    async def _llm(prompt: str) -> str:
        state["request_count"] += 1
        parsed = _valid_review(await base(prompt))
        state["parse_attempt_1"] = parsed is not None
        if parsed is not None:
            return json.dumps(parsed)
        state["repair_retry"] = True
        state["request_count"] += 1
        parsed2 = _valid_review(await base(prompt + _REPAIR_SUFFIX))
        state["parse_attempt_2"] = parsed2 is not None
        if parsed2 is not None:
            return json.dumps(parsed2)
        return json.dumps(
            {
                "decision": "ESCALATE",
                "reason": "reviewer output did not conform to SupervisorReview after repair",
            }
        )

    return _llm


__all__ = [
    "InternalSupervisor",
    "ReviewContext",
    "SupervisorUnavailable",
    "strict_reviewer_llm",
]
