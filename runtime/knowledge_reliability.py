"""Evidence-driven control adapter for existing knowledge/retrieval tools."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RetrievalPlan:
    question: str
    required_evidence: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=lambda: ["web"])
    depth: int = 1
    retrieval_budget: int = 5
    stop_condition: str = "required evidence covered"
    query_budget: int | None = None
    source_budget: int | None = None
    token_budget: int | None = None


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    query: str
    source: str
    content: str
    claim: str = ""
    authority: float = 0.5
    relevance: float = 0.5
    recency: float = 0.5
    source_type: str = "web"
    polarity: str = "支持"

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                {"source": self.source, "content": self.content, "claim": self.claim},
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()


@dataclass
class KnowledgeResult:
    status: str
    answer: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)
    retrievals: int = 0
    evidence_scores: dict[str, float] = field(default_factory=dict)
    source_diversity: int = 0


class KnowledgeRuntime:
    """Bounded retrieval loop using existing retriever callbacks."""

    def __init__(self, plan: RetrievalPlan):
        if plan.depth < 1 or plan.retrieval_budget < 1:
            raise ValueError("depth and retrieval_budget must be positive")
        self.plan = plan
        self.evidence: dict[str, Evidence] = {}
        self._retrieval_keys: set[str] = set()
        self.contradictions: list[dict[str, Any]] = []
        self.suppressed_retrievals = 0
        self._query_count = 0
        self._token_count = 0
        self._source_calls: set[str] = set()

    def _key(self, query: str, source_type: str, filter_value: str = "") -> str:
        return json.dumps([query, source_type, filter_value], ensure_ascii=False)

    def add_evidence(self, item: Evidence) -> bool:
        if item.fingerprint in {e.fingerprint for e in self.evidence.values()}:
            return False
        self.evidence[item.evidence_id] = item
        if item.claim:
            for other in self.evidence.values():
                if (
                    other.evidence_id != item.evidence_id
                    and other.claim == item.claim
                    and other.polarity != item.polarity
                ):
                    contradiction = {
                        "claim": item.claim,
                        "evidence_refs": [other.evidence_id, item.evidence_id],
                        "reason": "opposing source claims",
                    }
                    if contradiction not in self.contradictions:
                        self.contradictions.append(contradiction)
        return True

    def score(self, item: Evidence) -> float:
        # Explicit, inspectable weights: relevance and authority dominate;
        # recency is included but cannot silently erase older evidence.
        return 0.45 * item.relevance + 0.35 * item.authority + 0.20 * item.recency

    def enough(self) -> bool:
        if not self.plan.required_evidence:
            return bool(self.evidence)
        claims = {item.claim for item in self.evidence.values()}
        return all(
            any(required.lower() in claim.lower() for claim in claims)
            for required in self.plan.required_evidence
        )

    async def retrieve(
        self,
        retriever: Callable[[str, str, int], Awaitable[list[Evidence]] | list[Evidence]],
        *,
        next_query: Callable[[RetrievalPlan, list[Evidence]], str] | None = None,
    ) -> KnowledgeResult:
        query = self.plan.question
        for depth in range(1, self.plan.depth + 1):
            for source_type in self.plan.source_types:
                if self.enough():
                    self.suppressed_retrievals += 1
                    return self.result()
                if len(self._retrieval_keys) >= self.plan.retrieval_budget:
                    return self.result(status="PARTIAL")
                if (
                    self.plan.query_budget is not None
                    and self._query_count >= self.plan.query_budget
                ):
                    return self.result(status="PARTIAL")
                if (
                    self.plan.source_budget is not None
                    and len(self._source_calls) >= self.plan.source_budget
                    and source_type not in self._source_calls
                ):
                    return self.result(status="PARTIAL")
                key = self._key(query, source_type, str(depth))
                if key in self._retrieval_keys:
                    self.suppressed_retrievals += 1
                    continue
                self._retrieval_keys.add(key)
                self._query_count += 1
                self._source_calls.add(source_type)
                received = retriever(query, source_type, depth)
                items = await received if hasattr(received, "__await__") else received
                before = len(self.evidence)
                for item in items:
                    item_tokens = max(1, len(item.content) // 4)
                    if (
                        self.plan.token_budget is not None
                        and self._token_count + item_tokens > self.plan.token_budget
                    ):
                        return self.result(status="PARTIAL")
                    self._token_count += item_tokens
                    self.add_evidence(item)
                if len(self.evidence) == before:
                    self.suppressed_retrievals += 1
                if not self.enough() and next_query is not None:
                    query = next_query(self.plan, list(self.evidence.values()))
        return self.result(status="PARTIAL" if not self.enough() else "PASS")

    def result(self, *, status: str | None = None) -> KnowledgeResult:
        scores = {key: self.score(item) for key, item in self.evidence.items()}
        return KnowledgeResult(
            status=status or ("PASS" if self.enough() else "BLOCKED"),
            evidence_refs=list(self.evidence),
            contradictions=list(self.contradictions),
            uncertainty=[item["claim"] for item in self.contradictions],
            retrievals=len(self._retrieval_keys),
            evidence_scores=scores,
            source_diversity=len({item.source_type for item in self.evidence.values()}),
        )

    def synthesize(self, answer: str, evidence_refs: list[str]) -> KnowledgeResult:
        refs = list(dict.fromkeys(evidence_refs))
        if not refs or any(ref not in self.evidence for ref in refs):
            return KnowledgeResult(status="BLOCKED", contradictions=list(self.contradictions))
        result = self.result(status="PASS" if self.enough() else "PARTIAL")
        result.answer = answer
        result.evidence_refs = refs
        return result
