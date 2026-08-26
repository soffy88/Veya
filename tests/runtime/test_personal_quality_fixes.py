"""Deterministic correctness gates for Personal Runtime quality fixes."""

from __future__ import annotations

from runtime.personal.quality import (
    choose_skill_activation,
    evaluate_learning_gate,
    memory_eligibility,
    resolve_active_skill_version,
    resolve_memory_conflict,
    select_continuity_candidate,
)


def test_memory_stale_records_are_hard_excluded_but_audit_can_inspect():
    for status, reason in (
        ("superseded", "SUPERSEDED"),
        ("forgotten", "FORGOTTEN"),
        ("invalidated", "INVALIDATED"),
    ):
        decision = memory_eligibility(
            {
                "id": status,
                "status": status,
                "scope_type": "workspace",
                "scope_id": "ws",
                "confidence": 1.0,
            },
            workspace_id="ws",
        )
        assert decision.usable is False
        assert decision.exclusion_reason == reason

    successor = memory_eligibility(
        {
            "id": "new",
            "status": "active",
            "scope_type": "workspace",
            "scope_id": "ws",
            "confidence": 1.0,
        },
        workspace_id="ws",
    )
    assert successor.usable is True


def test_memory_conflict_precedence_and_unresolved_tie():
    old = {
        "id": "old",
        "status": "active",
        "scope_type": "user",
        "scope_id": "u",
        "confidence": 1.0,
        "updated_at": "2",
    }
    current = {
        "id": "current",
        "status": "active",
        "scope_type": "workspace",
        "scope_id": "ws",
        "confidence": 0.2,
        "updated_at": "1",
    }
    result = resolve_memory_conflict(
        [old, current], workspace_id="ws", explicit_correction_ids={"old"}
    )
    assert result.winner_id == "old"
    assert result.loser_ids == ("current",)

    tie = resolve_memory_conflict(
        [
            {
                "id": "a",
                "status": "active",
                "scope_type": "workspace",
                "scope_id": "ws",
                "confidence": 0.8,
                "updated_at": "1",
            },
            {
                "id": "b",
                "status": "active",
                "scope_type": "workspace",
                "scope_id": "ws",
                "confidence": 0.8,
                "updated_at": "1",
            },
        ],
        workspace_id="ws",
    )
    assert tie.winner_id is None
    assert tie.reason == "UNRESOLVED_CONFLICT"


def test_skill_activation_filters_scope_status_and_margin():
    wrong_workspace = {
        "id": "wrong",
        "status": "active",
        "trust_status": "trusted",
        "scope_type": "workspace",
        "scope_id": "other",
        "match_score": 1.0,
    }
    blocked = {
        "id": "blocked",
        "status": "blocked",
        "trust_status": "trusted",
        "scope_type": "workspace",
        "scope_id": "ws",
        "match_score": 1.0,
    }
    selected, decisions = choose_skill_activation([wrong_workspace, blocked], workspace_id="ws")
    assert selected is None
    assert {item.blocked_reason for item in decisions} == {"SCOPE_MISMATCH", "STATUS_NOT_ACTIVE"}

    ambiguous = [
        {
            "id": "a",
            "status": "active",
            "trust_status": "trusted",
            "scope_type": "workspace",
            "scope_id": "ws",
            "match_score": 0.80,
        },
        {
            "id": "b",
            "status": "active",
            "trust_status": "trusted",
            "scope_type": "workspace",
            "scope_id": "ws",
            "match_score": 0.75,
        },
    ]
    selected, decisions = choose_skill_activation(ambiguous, workspace_id="ws")
    assert selected is None
    assert (
        next(item for item in decisions if item.candidate_id == "a").reason == "NO_AUTO_ACTIVATION"
    )

    exact = dict(ambiguous[0], match_score=1.0)
    selected, _ = choose_skill_activation([exact], workspace_id="ws")
    assert selected is not None and selected.candidate_id == "a"


def test_skill_version_resolver_ignores_candidate_degraded_and_deprecated():
    versions = [
        {"version": 1, "status": "active", "trust_status": "trusted"},
        {"version": 2, "status": "candidate", "trust_status": "review_required"},
        {"version": 3, "status": "degraded", "trust_status": "trusted"},
        {"version": 4, "status": "deprecated", "trust_status": "trusted"},
    ]
    assert resolve_active_skill_version(versions)["version"] == 1


def test_learning_critical_regression_gate_is_fail_closed():
    assert (
        evaluate_learning_gate(
            {"candidate_score": 0.9, "baseline_score": 0.8, "critical_regression_count": 1}
        ).reason
        == "REJECT_CRITICAL_REGRESSION"
    )
    assert (
        evaluate_learning_gate(
            {
                "candidate_score": 0.9,
                "baseline_score": 0.8,
                "critical_scenario_results": [{"passed": False}],
            }
        ).reason
        == "REJECT_CRITICAL_REGRESSION"
    )
    assert (
        evaluate_learning_gate({"critical_evidence_required": True}).reason
        == "INSUFFICIENT_CRITICAL_EVIDENCE"
    )
    assert evaluate_learning_gate({"candidate_score": 0.9, "baseline_score": 0.8}).allowed is True


def test_continuity_prefers_unfinished_current_workspace_task_over_recent_completed():
    candidates = [
        {"task_id": "unfinished", "workspace_id": "ws", "status": "incomplete", "updated_at": "1"},
        {"task_id": "recent", "workspace_id": "ws", "status": "completed", "updated_at": "9"},
        {"task_id": "other", "workspace_id": "other", "status": "active", "updated_at": "9"},
    ]
    selected, scores = select_continuity_candidate(candidates, workspace_id="ws")
    assert selected is not None and selected["task_id"] == "unfinished"
    assert len(scores) == 3
