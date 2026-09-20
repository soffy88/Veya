"""Canonical Jev Decision Plane contracts (spec §15/§16).

Jev is a fast *classification* plane shared by both supervisors. It uses its
own provider protocol (``state + questions`` fan-out), never the OpenAI
chat/completions shape, and it has no execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class QuestionKind(StrEnum):
    choice = "choice"
    score = "score"
    noul = "noul"


class JevDecisionClass(StrEnum):
    """What Jev may be asked to classify (spec §15)."""

    evidence_sufficient = "evidence_sufficient"
    regression_risk = "regression_risk"
    retry_worthwhile = "retry_worthwhile"
    same_failure = "same_failure"
    safe_to_continue = "safe_to_continue"
    needs_supervisor_review = "needs_supervisor_review"
    blocker_class = "blocker_class"
    failure_class = "failure_class"
    next_action_class = "next_action_class"


@dataclass
class JevQuestion:
    """One fan-out question.

    Wire shapes differ per kind (learned from the live Zen schema):
    ``choice.criteria`` is a dict of option→description, ``score.criteria`` is a
    list of labels, and ``noul`` takes ``instructions`` (or criteria).
    """

    id: str
    kind: QuestionKind
    criteria: dict[str, str] | list[str] = field(default_factory=dict)
    instructions: str | None = None

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": str(self.kind)}
        if self.kind is QuestionKind.score:
            payload["criteria"] = list(self.criteria)
        elif self.kind is QuestionKind.choice:
            payload["criteria"] = dict(self.criteria)
        else:  # noul accepts criteria or instructions
            if self.criteria:
                payload["criteria"] = self.criteria
            if self.instructions:
                payload["instructions"] = self.instructions
        return payload


@dataclass
class JevAnswer:
    id: str
    kind: QuestionKind
    choice: str | None = None
    score: float | None = None
    noul: float | None = None
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_wire(cls, qid: str, answer: dict[str, Any]) -> JevAnswer:
        kind = QuestionKind(answer.get("type", "score"))
        return cls(
            id=qid,
            kind=kind,
            choice=answer.get("choice"),
            score=answer.get("score"),
            noul=answer.get("noul"),
            confidence=answer.get("confidence"),
            probabilities={
                str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": str(self.kind),
            "choice": self.choice,
            "score": self.score,
            "noul": self.noul,
            "confidence": self.confidence,
            "probabilities": dict(self.probabilities),
        }


@dataclass
class JevDecision:
    """A fan-out result. Never a verdict on the mission; only classification."""

    answers: dict[str, JevAnswer] = field(default_factory=dict)
    model: str = ""
    cost: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    source: str = "jev"  # "jev" | "provider_fallback"

    @property
    def confidence(self) -> float | None:
        values = [a.confidence for a in self.answers.values() if a.confidence is not None]
        return min(values) if values else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "answers": {k: v.to_dict() for k, v in self.answers.items()},
            "model": self.model,
            "cost": self.cost,
            "usage": dict(self.usage),
            "source": self.source,
            "confidence": self.confidence,
        }
