"""goal_run trust_plane 测试(VAOM Trust Plane P1 落地, 见 docs/dev/rfc-01-vaom.md)。"""

from __future__ import annotations

from server.goal_run.trust_plane import (
    append_trust_plane_records,
    build_and_write_task_episode,
    read_task_episode,
    read_trust_plane_records,
    record_task_verification,
)


def test_record_task_verification_pass_produces_verified_state():
    claim, evidences, evaluations, verified_state = record_task_verification(
        task_id="t1",
        goal_id="g1",
        actor="hicode",
        statement="did the thing",
        target_refs=["file exists"],
        verify_passed=True,
        verify_summary="1 passed, 0 failed",
        diff_text="diff --git a/x.py b/x.py\n+pass\n",
        review_findings={
            "standards": {"findings": [], "worst": None},
            "spec": {"findings": [{"requirement": "r1", "detail": "d1"}], "worst": "missing r1"},
        },
    )

    assert claim.status == "verified"
    assert claim.task_id == "t1" and claim.goal_id == "g1"

    # 两条 evidence: git_diff + verify summary log
    kinds = {e.kind for e in evidences}
    assert kinds == {"git_diff", "log"}
    assert all(e.claim_id == claim.claim_id for e in evidences)
    assert all(e.hash for e in evidences)  # __post_init__ 自动 hash

    # 1 条 E0 + 2 条 E2(standards/spec 各一条)
    e0 = [e for e in evaluations if e.evaluator_type == "E0_deterministic"]
    e2 = [e for e in evaluations if e.evaluator_type == "E2_independent_model"]
    assert len(e0) == 1 and e0[0].verdict == "pass"
    assert len(e2) == 2
    spec_eval = next(e for e in e2 if e.rubric_metrics.get("axis") == "spec")
    assert spec_eval.failures == ["missing r1"]

    assert verified_state is not None
    assert verified_state.status == "verified"
    assert verified_state.claim_id == claim.claim_id
    assert set(verified_state.evidence_ids) == {e.evidence_id for e in evidences}
    assert set(verified_state.evaluation_ids) == {e.eval_id for e in evaluations}


def test_record_task_verification_fail_produces_no_verified_state():
    claim, evidences, evaluations, verified_state = record_task_verification(
        task_id="t2",
        goal_id="g1",
        actor="hicode",
        statement="tried the thing",
        target_refs=["tests pass"],
        verify_passed=False,
        verify_summary="2 passed, 1 failed",
        diff_text="",
        review_findings=None,
    )

    assert claim.status == "rejected"
    assert verified_state is None
    # 没有 diff, 只有 verify_summary 一条 evidence(log)
    assert [e.kind for e in evidences] == ["log"]
    assert len(evaluations) == 1
    assert evaluations[0].verdict == "fail"
    assert evaluations[0].failures == ["2 passed, 1 failed"]


def test_append_and_read_trust_plane_records_roundtrip(tmp_path):
    project_root = str(tmp_path)
    claim, evidences, evaluations, verified_state = record_task_verification(
        task_id="t1",
        goal_id="g-roundtrip",
        actor="hicode",
        statement="s",
        target_refs=[],
        verify_passed=True,
        verify_summary="ok",
        diff_text="d",
        review_findings=None,
    )
    append_trust_plane_records(
        project_root,
        "g-roundtrip",
        claim=claim,
        evidences=evidences,
        evaluations=evaluations,
        verified_state=verified_state,
    )

    records = read_trust_plane_records(project_root, "g-roundtrip")
    types = [r["_type"] for r in records]
    assert types.count("Claim") == 1
    assert types.count("Evidence") == len(evidences)
    assert types.count("EvaluationResult") == len(evaluations)
    assert types.count("VerifiedState") == 1

    claim_record = next(r for r in records if r["_type"] == "Claim")
    assert claim_record["claim_id"] == claim.claim_id
    assert claim_record["status"] == "verified"


def test_read_trust_plane_records_missing_file_returns_empty(tmp_path):
    assert read_trust_plane_records(str(tmp_path), "nonexistent") == []


def test_build_and_write_task_episode_aggregates_ids(tmp_path):
    project_root = str(tmp_path)
    goal_id = "g-episode"

    for tid, passed in (("t1", True), ("t2", False)):
        claim, evidences, evaluations, verified_state = record_task_verification(
            task_id=tid,
            goal_id=goal_id,
            actor="hicode",
            statement="s",
            target_refs=[],
            verify_passed=passed,
            verify_summary="ok" if passed else "fail",
            diff_text="d" if passed else "",
            review_findings=None,
        )
        append_trust_plane_records(
            project_root,
            goal_id,
            claim=claim,
            evidences=evidences,
            evaluations=evaluations,
            verified_state=verified_state,
        )

    episode = build_and_write_task_episode(
        project_root,
        goal_id,
        "do the thing",
        task_ids=["t1", "t2"],
        outcome="completed",
        started_at="2026-08-23T00:00:00+00:00",
        completed_at="2026-08-23T00:05:00+00:00",
    )

    assert episode.goal_id == goal_id
    assert episode.task_ids == ["t1", "t2"]
    assert len(episode.claim_ids) == 2
    # 只有 t1(passed) 产生 VerifiedState
    assert len(episode.verified_state_ids) == 1
    assert episode.outcome == "completed"

    reloaded = read_task_episode(project_root, goal_id)
    assert reloaded is not None
    assert reloaded["episode_id"] == episode.episode_id
    assert reloaded["claim_ids"] == episode.claim_ids


def test_read_task_episode_missing_returns_none(tmp_path):
    assert read_task_episode(str(tmp_path), "nonexistent") is None
