"""Deterministic correctness policies shared by Personal Runtime and Gold replay.

These helpers are intentionally policy-only.  They do not route the MasterAgent,
search memory automatically, or choose whether a capability should be called.
They make the result of an already requested memory/skill/continuity/learning
operation safe, scoped, and auditable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_MEMORY_TERMINAL_EXCLUSIONS = {
    "superseded": "SUPERSEDED",
    "forgotten": "FORGOTTEN",
    "invalidated": "INVALIDATED",
}
_SKILL_INACTIVE = {"candidate", "deprecated", "degraded", "rolled_back", "blocked"}
_TRUSTED = "trusted"


def _stem(token: str) -> str:
    token = token.lower()
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def content_tokens(value: str) -> set[str]:
    return {
        _stem(token)
        for token in re.findall(r"[a-z0-9]+", str(value).lower())
        if token
        not in {
            "a",
            "an",
            "and",
            "are",
            "do",
            "for",
            "here",
            "in",
            "is",
            "of",
            "on",
            "or",
            "the",
            "this",
            "to",
            "use",
            "what",
            "with",
        }
    }


def memory_relevance(query: str, content: str) -> float:
    query_tokens = content_tokens(query)
    if not query_tokens:
        return 1.0
    content_set = content_tokens(content)
    return len(query_tokens & content_set) / len(query_tokens)


@dataclass(frozen=True)
class MemoryEligibilityDecision:
    memory_id: str
    usable: bool
    exclusion_reason: str | None
    scope_match: bool
    relevance: float


def memory_eligibility(
    record: Mapping[str, Any],
    *,
    query: str = "",
    workspace_id: str | None = None,
    user_id: str | None = None,
    min_confidence: float = 0.0,
    conflict_loser_ids: set[str] | frozenset[str] = frozenset(),
) -> MemoryEligibilityDecision:
    memory_id = str(record.get("id", ""))
    status = str(record.get("status", ""))
    if status in _MEMORY_TERMINAL_EXCLUSIONS:
        return MemoryEligibilityDecision(
            memory_id, False, _MEMORY_TERMINAL_EXCLUSIONS[status], False, 0.0
        )
    if memory_id in conflict_loser_ids:
        return MemoryEligibilityDecision(memory_id, False, "CONFLICT_LOSER", False, 0.0)
    scope_type = str(record.get("scope_type", ""))
    scope_id = str(record.get("scope_id", ""))
    scope_match = True
    if scope_type == "workspace" and workspace_id is not None:
        scope_match = scope_id == str(workspace_id)
    elif scope_type == "user" and user_id is not None:
        scope_match = scope_id == str(user_id)
    if not scope_match:
        return MemoryEligibilityDecision(memory_id, False, "SCOPE_MISMATCH", False, 0.0)
    try:
        confidence = float(record.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < float(min_confidence):
        return MemoryEligibilityDecision(memory_id, False, "LOW_CONFIDENCE", scope_match, 0.0)
    relevance = memory_relevance(query, str(record.get("content", "")))
    if query and relevance <= 0:
        return MemoryEligibilityDecision(memory_id, False, "LOW_RELEVANCE", scope_match, relevance)
    return MemoryEligibilityDecision(memory_id, True, None, scope_match, relevance)


@dataclass(frozen=True)
class ConflictResolutionResult:
    winner_id: str | None
    loser_ids: tuple[str, ...]
    reason: str
    evidence: tuple[str, ...]


def resolve_memory_conflict(
    records: list[Mapping[str, Any]],
    *,
    workspace_id: str | None = None,
    explicit_correction_ids: set[str] | frozenset[str] = frozenset(),
    explicit_supersede_ids: set[str] | frozenset[str] = frozenset(),
) -> ConflictResolutionResult:
    """Resolve one already-related memory set deterministically.

    The ordering is authority-first.  A newer timestamp cannot beat an
    explicit correction or supersede edge.
    """
    if not records:
        return ConflictResolutionResult(None, (), "NO_RECORDS", ())

    def score(record: Mapping[str, Any]) -> tuple[int, int, int, float, str]:
        rid = str(record.get("id", ""))
        status = str(record.get("status", ""))
        explicit = (
            2 if rid in explicit_correction_ids else 1 if rid in explicit_supersede_ids else 0
        )
        scope = (
            1
            if workspace_id is not None
            and record.get("scope_type") == "workspace"
            and str(record.get("scope_id")) == str(workspace_id)
            else 0
        )
        verified = 1 if record.get("last_verified_at") else 0
        try:
            confidence = float(record.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        # Active records beat non-active records before confidence/recency.
        active = 1 if status == "active" else 0
        return (explicit, scope, active + verified, confidence, str(record.get("updated_at", "")))

    active = [record for record in records if str(record.get("status")) == "active"]
    pool = active or list(records)
    scored = [(record, score(record)) for record in pool]
    best_score = max(item_score for _, item_score in scored)
    best = [record for record, item_score in scored if item_score == best_score]
    if len(best) > 1:
        ids = tuple(sorted(str(record.get("id")) for record in best))
        return ConflictResolutionResult(None, ids, "UNRESOLVED_CONFLICT", ids)
    winner = best[0]
    winner_id = str(winner.get("id"))
    losers = tuple(
        sorted(str(record.get("id")) for record in records if str(record.get("id")) != winner_id)
    )
    reasons = []
    if winner_id in explicit_correction_ids:
        reasons.append("explicit_correction")
    if winner_id in explicit_supersede_ids:
        reasons.append("explicit_supersede")
    if workspace_id is not None and winner.get("scope_type") == "workspace":
        reasons.append("workspace_specific")
    reasons.append("active_authority_then_confidence_recency")
    return ConflictResolutionResult(winner_id, losers, "+".join(reasons), (winner_id, *losers))


@dataclass(frozen=True)
class SkillActivationDecision:
    candidate_id: str
    eligible: bool
    score: float
    activation_margin: float | None
    reason: str
    blocked_reason: str | None = None


def skill_eligibility(
    candidate: Mapping[str, Any],
    *,
    workspace_id: str | None = None,
    user_id: str | None = None,
    available_capabilities: set[str] | frozenset[str] | None = None,
) -> SkillActivationDecision:
    candidate_id = str(candidate.get("id") or candidate.get("skill_id") or "")
    scope_type = str(candidate.get("scope_type", ""))
    scope_id = str(candidate.get("scope_id", ""))
    if str(candidate.get("status", "")) != "active":
        return SkillActivationDecision(
            candidate_id, False, 0.0, None, "INELIGIBLE", "STATUS_NOT_ACTIVE"
        )
    if str(candidate.get("trust_status", "")) != _TRUSTED:
        return SkillActivationDecision(
            candidate_id, False, 0.0, None, "INELIGIBLE", "TRUST_NOT_TRUSTED"
        )
    if scope_type == "workspace" and workspace_id is not None and scope_id != str(workspace_id):
        return SkillActivationDecision(
            candidate_id, False, 0.0, None, "INELIGIBLE", "SCOPE_MISMATCH"
        )
    if scope_type == "user" and user_id is not None and scope_id != str(user_id):
        return SkillActivationDecision(
            candidate_id, False, 0.0, None, "INELIGIBLE", "SCOPE_MISMATCH"
        )
    required = set(candidate.get("required_capabilities") or candidate.get("capabilities") or [])
    if available_capabilities is not None and not required <= set(available_capabilities):
        return SkillActivationDecision(
            candidate_id, False, 0.0, None, "INELIGIBLE", "CAPABILITY_UNAVAILABLE"
        )
    try:
        score = float(candidate.get("match_score", candidate.get("score", 0.0)))
    except (TypeError, ValueError):
        score = 0.0
    return SkillActivationDecision(candidate_id, True, score, None, "ELIGIBLE")


def choose_skill_activation(
    candidates: list[Mapping[str, Any]],
    *,
    workspace_id: str | None = None,
    user_id: str | None = None,
    activation_threshold: float = 0.75,
    minimum_margin: float = 0.10,
    available_capabilities: set[str] | frozenset[str] | None = None,
) -> tuple[SkillActivationDecision | None, list[SkillActivationDecision]]:
    decisions = [
        skill_eligibility(
            candidate,
            workspace_id=workspace_id,
            user_id=user_id,
            available_capabilities=available_capabilities,
        )
        for candidate in candidates
    ]
    eligible = [decision for decision in decisions if decision.eligible]
    eligible.sort(key=lambda decision: (-decision.score, decision.candidate_id))
    if not eligible:
        return None, decisions
    top = eligible[0]
    second_score = eligible[1].score if len(eligible) > 1 else None
    margin = top.score - second_score if second_score is not None else None
    top = SkillActivationDecision(
        top.candidate_id,
        True,
        top.score,
        margin,
        "ACTIVATE"
        if top.score >= activation_threshold and (margin is None or margin >= minimum_margin)
        else "NO_AUTO_ACTIVATION",
        None
        if top.score >= activation_threshold and (margin is None or margin >= minimum_margin)
        else "AMBIGUOUS_OR_BELOW_THRESHOLD",
    )
    return (top if top.reason == "ACTIVATE" else None), [
        top if decision.candidate_id == top.candidate_id else decision for decision in decisions
    ]


def resolve_active_skill_version(versions: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Choose the active trusted non-degraded version, never max(version) blindly."""
    eligible = [
        version
        for version in versions
        if str(version.get("status")) == "active" and str(version.get("trust_status")) == _TRUSTED
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda version: int(version.get("version", 0)))


@dataclass(frozen=True)
class ContinuityCandidateScore:
    task_id: str
    explicit_match: bool
    workspace_match: bool
    task_state_rank: int
    updated_at: str

    @property
    def key(self) -> tuple[int, int, int, str]:
        return (
            int(self.explicit_match),
            int(self.workspace_match),
            self.task_state_rank,
            self.updated_at,
        )


def score_continuity_candidate(
    candidate: Mapping[str, Any],
    *,
    workspace_id: str | None = None,
    explicit_task_id: str | None = None,
) -> ContinuityCandidateScore:
    state = str(candidate.get("status") or candidate.get("state") or "")
    state_rank = {"active": 3, "paused": 2, "incomplete": 1, "running": 3}.get(state, 0)
    return ContinuityCandidateScore(
        task_id=str(candidate.get("task_id") or candidate.get("id") or ""),
        explicit_match=bool(
            explicit_task_id
            and str(candidate.get("task_id") or candidate.get("id")) == str(explicit_task_id)
        ),
        workspace_match=workspace_id is None
        or str(candidate.get("workspace_id")) == str(workspace_id),
        task_state_rank=state_rank,
        updated_at=str(candidate.get("updated_at") or ""),
    )


def select_continuity_candidate(
    candidates: list[Mapping[str, Any]],
    *,
    workspace_id: str | None = None,
    explicit_task_id: str | None = None,
) -> tuple[Mapping[str, Any] | None, list[ContinuityCandidateScore]]:
    scored = [
        score_continuity_candidate(
            item, workspace_id=workspace_id, explicit_task_id=explicit_task_id
        )
        for item in candidates
    ]
    eligible = [
        (item, score)
        for item, score in zip(candidates, scored, strict=True)
        if score.workspace_match
    ]
    if not eligible:
        return None, scored
    eligible.sort(key=lambda pair: pair[1].key, reverse=True)
    if len(eligible) > 1 and eligible[0][1].key == eligible[1][1].key:
        return None, scored
    return eligible[0][0], scored


@dataclass(frozen=True)
class LearningGateDecision:
    allowed: bool
    reason: str
    critical_regression_count: int


def evaluate_learning_gate(
    result: Mapping[str, Any], *, requested_pass: bool = True
) -> LearningGateDecision:
    if not requested_pass:
        return LearningGateDecision(False, "REJECTED_BY_EVAL", 0)
    critical_count = int(result.get("critical_regression_count", 0) or 0)
    if result.get("critical_regression") is True:
        critical_count = max(1, critical_count)
    critical_results = result.get("critical_scenario_results")
    if result.get("critical_evidence_required") and not isinstance(critical_results, list):
        return LearningGateDecision(False, "INSUFFICIENT_CRITICAL_EVIDENCE", 0)
    if isinstance(critical_results, list):
        critical_count = max(
            critical_count,
            sum(
                1
                for item in critical_results
                if item is False or (isinstance(item, Mapping) and item.get("passed") is False)
            ),
        )
    if critical_count > 0:
        return LearningGateDecision(False, "REJECT_CRITICAL_REGRESSION", critical_count)
    return LearningGateDecision(True, "VALIDATED", 0)
