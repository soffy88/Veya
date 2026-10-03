"""P0-09 Evaluation Plane — quality/cost/latency evaluation, not observability."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class EvaluatorType(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    TEST = "TEST"
    STATIC = "STATIC"
    RUBRIC = "RUBRIC"
    MODEL = "MODEL"
    HUMAN = "HUMAN"
    PRODUCT_PROBE = "PRODUCT_PROBE"


class EvaluationDimension(StrEnum):
    TASK_SUCCESS = "task_success"
    VERIFICATION_PASS_RATE = "verification_pass_rate"
    FALSE_SUCCESS_RATE = "false_success_rate"
    TOOL_CORRECTNESS = "tool_correctness"
    RECOVERY_SUCCESS = "recovery_success"
    DUPLICATE_SIDE_EFFECT_RATE = "duplicate_side_effect_rate"
    QUALITY = "quality"
    LATENCY = "latency"
    TOKEN_USAGE = "token_usage"
    COST = "cost"
    HUMAN_INTERVENTION_RATE = "human_intervention_rate"
    POLICY_VIOLATION_RATE = "policy_violation_rate"


@dataclass(frozen=True)
class EvaluationCase:
    """A single evaluation case."""

    case_id: str
    name: str
    evaluator_type: EvaluatorType
    dimensions: tuple[EvaluationDimension, ...] = ()
    input_data: dict[str, Any] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evaluator_type"] = self.evaluator_type.value
        data["dimensions"] = [d.value for d in self.dimensions]
        return data


@dataclass(frozen=True)
class EvaluationSuite:
    """A suite of evaluation cases."""

    suite_id: str
    name: str
    cases: tuple[EvaluationCase, ...] = ()
    version: str = "1.0.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_id": self.suite_id,
            "name": self.name,
            "cases": [c.to_dict() for c in self.cases],
            "version": self.version,
        }


@dataclass(frozen=True)
class EvaluationResult:
    """Result of running an evaluation case."""

    result_id: str
    case_id: str
    run_id: str
    dimension: EvaluationDimension
    score: float
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["dimension"] = self.dimension.value
        return data


@dataclass(frozen=True)
class EvaluationRun:
    """A single evaluation run with version attribution."""

    run_id: str
    suite_id: str
    results: tuple[EvaluationResult, ...] = ()
    model: str = ""
    provider: str = ""
    model_config: dict[str, Any] = field(default_factory=dict)
    agent_version: str = ""
    skill_versions: dict[str, str] = field(default_factory=dict)
    workflow_versions: dict[str, str] = field(default_factory=dict)
    harness_version: str = ""
    policy_version: str = ""
    context_policy_version: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "suite_id": self.suite_id,
            "results": [r.to_dict() for r in self.results],
            "model": self.model,
            "provider": self.provider,
            "model_config": self.model_config,
            "agent_version": self.agent_version,
            "skill_versions": self.skill_versions,
            "workflow_versions": self.workflow_versions,
            "harness_version": self.harness_version,
            "policy_version": self.policy_version,
            "context_policy_version": self.context_policy_version,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Baseline:
    """A baseline for regression comparison."""

    baseline_id: str
    suite_id: str
    run_id: str
    scores: dict[str, float] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvaluationGate:
    """Promotion gate: candidate → suite → compare baseline → QUALIFY/REJECT."""

    gate_id: str
    suite_id: str
    baseline_id: str
    candidate_run_id: str
    verdict: str = ""
    reason: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def new_suite(name: str, cases: list[EvaluationCase]) -> EvaluationSuite:
    return EvaluationSuite(
        suite_id=str(uuid.uuid4()),
        name=name,
        cases=tuple(cases),
    )


def new_case(
    name: str,
    evaluator_type: EvaluatorType,
    dimensions: list[EvaluationDimension] | None = None,
) -> EvaluationCase:
    return EvaluationCase(
        case_id=str(uuid.uuid4()),
        name=name,
        evaluator_type=evaluator_type,
        dimensions=tuple(dimensions or []),
    )


def new_result(
    case_id: str,
    run_id: str,
    dimension: EvaluationDimension,
    score: float,
    *,
    passed: bool = True,
) -> EvaluationResult:
    return EvaluationResult(
        result_id=str(uuid.uuid4()),
        case_id=case_id,
        run_id=run_id,
        dimension=dimension,
        score=score,
        passed=passed,
    )


def compare_baseline(
    candidate: EvaluationRun,
    baseline: Baseline,
    *,
    threshold: float = 0.05,
) -> EvaluationGate:
    """Compare candidate against baseline. Regression → REJECT."""
    regressions = []
    for result in candidate.results:
        dim_key = result.dimension.value
        base_score = baseline.scores.get(dim_key)
        if base_score is not None and result.score < base_score - threshold:
            regressions.append(dim_key)
    if regressions:
        return EvaluationGate(
            gate_id=str(uuid.uuid4()),
            suite_id=candidate.suite_id,
            baseline_id=baseline.baseline_id,
            candidate_run_id=candidate.run_id,
            verdict="REJECT",
            reason=f"regressions: {', '.join(regressions)}",
        )
    return EvaluationGate(
        gate_id=str(uuid.uuid4()),
        suite_id=candidate.suite_id,
        baseline_id=baseline.baseline_id,
        candidate_run_id=candidate.run_id,
        verdict="QUALIFY",
        reason="no regressions detected",
    )
