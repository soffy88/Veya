"""D3 gates: canonical resume-disposition policy (Veya project policy).

Matrix: user-rerun freshness, infra continuity, checkpoint recovery rules,
transient/permanent/unknown rejection handling, capability/session guards,
idempotency, restart survival, and real wiring through CodingTaskService
+ LongRunningHarness. No skips, no swallowed exceptions.
"""

from __future__ import annotations

import pytest

from runtime.execution.models import ExecutionCheckpoint
from runtime.execution.resume import (
    RESUME_DECIDED_TOPIC,
    ResumeDecision,
    ResumeDecisionStore,
    ResumeDisposition,
    ResumeTrigger,
    decide_resume_disposition,
    record_resume_decision,
    rejection_evidence_from_harness,
    verify_execution_checkpoint,
)


def _decide(**kwargs):
    return decide_resume_disposition(**kwargs)


# -- vocabulary -------------------------------------------------------------


def test_disposition_values():
    assert {m.value for m in ResumeDisposition} == {
        "resume_session",
        "recover_checkpoint",
        "fresh_transient",
        "fresh_permanent",
        "fresh_user_retry",
        "blocked",
    }
    assert RESUME_DECIDED_TOPIC == "execution.resume_decided"


def test_trigger_unknown_fail_closed():
    with pytest.raises(ValueError):
        _decide(trigger="teleport")
    assert ResumeTrigger.coerce("USER_RERUN") is ResumeTrigger.USER_RERUN


def test_decision_object_contract():
    decision = ResumeDecision(
        disposition="resume_session",
        reason="r",
        trigger="infra_retry",
        session_id="s",
    )
    assert decision.disposition is ResumeDisposition.RESUME_SESSION
    assert decision.preserve_original_session is True
    assert decision.retire_session is False
    clone = ResumeDecision.from_dict(decision.to_dict())
    assert clone.key() == decision.key()
    with pytest.raises(ValueError):
        ResumeDecision(disposition="resume_session", reason="", trigger="infra_retry")
    with pytest.raises(ValueError):
        ResumeDecision(disposition="nope", reason="r", trigger="infra_retry")


# -- policy matrix ----------------------------------------------------------


def test_user_rerun_always_fresh_without_replay():
    decision = _decide(trigger="user_rerun", session_id="old", task_id="t")
    assert decision.disposition is ResumeDisposition.FRESH_USER_RETRY
    assert decision.session_id == "old"
    assert decision.retire_session is False
    assert decision.preserve_original_session is True


def test_infra_retry_keeps_continuity():
    decision = _decide(trigger="infra_retry", session_id="s", resume_capable=True, run_id="g")
    assert decision.disposition is ResumeDisposition.RESUME_SESSION


def test_infra_retry_without_capability_never_resumes():
    decision = _decide(trigger="infra_retry", session_id="s", resume_capable=False)
    assert decision.disposition is ResumeDisposition.BLOCKED


def test_infra_retry_without_session_fresh_attempt():
    decision = _decide(trigger="infra_retry")
    assert decision.disposition is ResumeDisposition.FRESH_TRANSIENT
    assert decision.retire_session is False


def test_process_recovery_verified_checkpoint():
    decision = _decide(
        trigger="process_recovery",
        checkpoint_id="cp-1",
        checkpoint_verified=True,
        checkpoint_lineage_match=True,
    )
    assert decision.disposition is ResumeDisposition.RECOVER_CHECKPOINT


def test_process_recovery_never_recovers_unverified():
    bad = _decide(
        trigger="process_recovery",
        checkpoint_id="cp-1",
        checkpoint_verified=False,
        checkpoint_lineage_match=True,
    )
    assert bad.disposition is ResumeDisposition.BLOCKED
    foreign = _decide(
        trigger="process_recovery",
        checkpoint_id="cp-1",
        checkpoint_verified=True,
        checkpoint_lineage_match=False,
    )
    assert foreign.disposition is ResumeDisposition.BLOCKED


def test_process_recovery_session_fallback():
    decision = _decide(trigger="process_recovery", session_id="s", resume_capable=True)
    assert decision.disposition is ResumeDisposition.RESUME_SESSION
    assert _decide(trigger="process_recovery").disposition is ResumeDisposition.BLOCKED


def test_rejection_evidence_branches():
    transient = _decide(trigger="resume_rejected", session_id="s", rejection_evidence="transient")
    assert transient.disposition is ResumeDisposition.FRESH_TRANSIENT
    assert transient.retire_session is False
    assert transient.preserve_original_session is True

    permanent = _decide(trigger="resume_rejected", session_id="s", rejection_evidence="permanent")
    assert permanent.disposition is ResumeDisposition.FRESH_PERMANENT
    assert permanent.retire_session is True
    assert permanent.preserve_original_session is False

    unknown = _decide(trigger="resume_rejected", session_id="s")
    assert unknown.disposition is ResumeDisposition.BLOCKED


def test_retired_session_never_resumes_again():
    first = _decide(trigger="resume_rejected", session_id="s", rejection_evidence="permanent")
    assert first.retire_session is True
    again = _decide(
        trigger="infra_retry",
        session_id="s",
        resume_capable=True,
        retired_sessions={"s"},
    )
    assert again.disposition is ResumeDisposition.FRESH_PERMANENT
    assert again.retire_session is True


def test_manual_resume_paths():
    assert (
        _decide(trigger="manual_resume", session_id="s", resume_capable=True).disposition
        is ResumeDisposition.RESUME_SESSION
    )
    assert (
        _decide(
            trigger="manual_resume",
            checkpoint_id="cp-1",
            checkpoint_lineage_match=True,
        ).disposition
        is ResumeDisposition.RECOVER_CHECKPOINT
    )
    assert _decide(trigger="manual_resume").disposition is ResumeDisposition.BLOCKED


# -- harness fact bridge ----------------------------------------------------


def test_rejection_evidence_from_harness_facts():
    assert rejection_evidence_from_harness("resume_rejected", "session not found") == "permanent"
    assert rejection_evidence_from_harness("resume_rejected", "session expired") == "permanent"
    assert rejection_evidence_from_harness("timeout", "wait timed out") == "transient"
    assert rejection_evidence_from_harness("resume_rejected", "weird blob") is None
    assert rejection_evidence_from_harness(None, None) is None


# -- checkpoint verification ------------------------------------------------


def test_verify_execution_checkpoint():
    assert verify_execution_checkpoint(None) == (False, "no-checkpoint")
    good = ExecutionCheckpoint(
        event_cursor="e-1",
        scheduler_snapshot={"goal_id": "g"},
        lineage_id="g",
        verified=True,
    )
    assert verify_execution_checkpoint(good, expected_lineage_id="g") == (True, "verified")
    assert verify_execution_checkpoint(good, expected_lineage_id="other") == (
        False,
        "lineage-mismatch",
    )
    legacy = ExecutionCheckpoint(event_cursor="e-1", scheduler_snapshot={"goal_id": "g"})
    assert verify_execution_checkpoint(legacy, expected_lineage_id="g") == (
        False,
        "unverified",
    )
    assert verify_execution_checkpoint(legacy) == (False, "unverified")
    bad_version = ExecutionCheckpoint(
        event_cursor="e-1",
        scheduler_snapshot={},
        lineage_id="g",
        verified=True,
        schema_version=99,
    )
    assert verify_execution_checkpoint(bad_version) == (False, "version-mismatch")


# -- persistence / idempotency / restart ------------------------------------


def test_store_record_dedupes_and_restart_survives(tmp_path):
    store = ResumeDecisionStore(tmp_path / "run")
    decision = _decide(trigger="user_rerun", session_id="s", task_id="t")
    first = store.record(decision)
    second = store.record(decision)
    assert first["key"] == second["key"] == decision.key()
    assert len(store._load()["decisions"]) == 1

    reopened = ResumeDecisionStore(tmp_path / "run")
    assert reopened.last(task_id="t")["key"] == decision.key()
    assert reopened.last(task_id="other") is None


def test_store_retirement_idempotent(tmp_path):
    store = ResumeDecisionStore(tmp_path / "run")
    decision = _decide(trigger="resume_rejected", session_id="s", rejection_evidence="permanent")
    store.record(decision)
    store.record(decision)
    assert store.is_retired("s") is True
    assert store.retired_sessions() == ["s"]
    assert len(store._load()["decisions"]) == 1


def test_decision_key_stable_for_same_evidence():
    left = _decide(trigger="infra_retry", session_id="s", resume_capable=True)
    right = _decide(trigger="infra_retry", session_id="s", resume_capable=True)
    assert left.key() == right.key()
    assert left.disposition == right.disposition


def test_record_emits_canonical_topic():
    events: list[tuple[str, dict]] = []
    decision = _decide(trigger="infra_retry", session_id="s", resume_capable=True)
    record_resume_decision(decision, emit=lambda topic, payload: events.append((topic, payload)))
    assert len(events) == 1
    topic, payload = events[0]
    assert topic == "execution.resume_decided"
    assert payload["disposition"] == "resume_session"
    assert payload["key"] == decision.key()


# -- real wiring: coding task service ---------------------------------------


def _write_coding_state(project_root, task_id, status, goal_run_id="goal-1"):
    from runtime.coding.task_service import CodingTaskState, _write_task_state

    state = CodingTaskState(
        task_id=task_id,
        workspace_path=str(project_root),
        workspace_id="ws",
        objective="obj",
        source="test",
        status=status,
        goal_run_id=goal_run_id,
        worktree_path=str(project_root / "wt"),
    )
    _write_task_state(project_root, state)
    return state


@pytest.mark.asyncio()
async def test_resume_task_blocks_failed_without_user_intent(tmp_path):
    from runtime.coding.task_service import CodingTaskService, WorktreeError

    _write_coding_state(tmp_path, "t-failed", "failed")
    service = CodingTaskService(tmp_path)
    with pytest.raises(WorktreeError, match="Resume refused"):
        await service.resume_task("t-failed")
    from runtime.execution.resume import ResumeDecisionStore

    stored = ResumeDecisionStore(tmp_path / ".veya" / "runs" / "t-failed").last(task_id="t-failed")
    assert stored is not None
    assert stored["disposition"] == "blocked"


@pytest.mark.asyncio()
async def test_resume_task_recovers_running_state(tmp_path):
    from runtime.coding.task_service import CodingTaskService

    _write_coding_state(tmp_path, "t-run", "running")
    service = CodingTaskService(tmp_path)
    result = await service.resume_task("t-run")
    assert result.resume_decision is not None
    assert result.resume_decision["disposition"] == "recover_checkpoint"


@pytest.mark.asyncio()
async def test_user_rerun_never_reuses_old_goal(tmp_path):
    from runtime.coding.task_service import CodingTaskResult, CodingTaskService

    _write_coding_state(tmp_path, "t-bad", "failed", goal_run_id="old-goal")
    service = CodingTaskService(tmp_path)
    seen: dict[str, object] = {}

    async def fake_run(task_id, leaves, *, goal_run_id=None):
        seen["goal_run_id"] = goal_run_id
        return CodingTaskResult(task_id, None, "completed", None, [], [], "fake", True)

    service.run_task = fake_run  # type: ignore[method-assign]
    result = await service.resume_task("t-bad", [{"tool": "x", "args": {}}], trigger="user_rerun")
    assert seen["goal_run_id"] is None
    assert result.resume_decision is not None
    assert result.resume_decision["disposition"] == "fresh_user_retry"
    assert result.resume_decision["retire_session"] is False


# -- real wiring: long-running harness --------------------------------------


def test_harness_resume_missing_checkpoint_is_permanent(tmp_path):
    from runtime.execution.long_running import (
        HarnessError,
        LongRunCheckpointStore,
        LongRunningHarness,
    )
    from runtime.execution.resume import ResumeDecisionStore

    store = LongRunCheckpointStore(tmp_path / "run")
    with pytest.raises(HarnessError, match="fresh_permanent"):
        LongRunningHarness.resume(store)
    run_root = tmp_path / "run"
    recorded = ResumeDecisionStore(run_root).last()
    assert recorded is not None
    assert recorded["disposition"] == "fresh_permanent"


def test_harness_resume_invalid_checkpoint_blocks(tmp_path):
    from runtime.execution.long_running import (
        HarnessError,
        LongRunCheckpointStore,
        LongRunningHarness,
    )
    from runtime.execution.resume import ResumeDecisionStore

    path = tmp_path / "run" / "checkpoints" / "long-running.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupt", encoding="utf-8")
    store = LongRunCheckpointStore(tmp_path / "run")
    with pytest.raises(HarnessError, match="blocked"):
        LongRunningHarness.resume(store)
    recorded = ResumeDecisionStore(tmp_path / "run").last()
    assert recorded is not None
    assert recorded["disposition"] == "blocked"


def test_harness_resume_recovers_verified_lineage(tmp_path):
    from runtime.execution.long_running import (
        LongRunCheckpointStore,
        LongRunningHarness,
        LongRunState,
    )

    store = LongRunCheckpointStore(tmp_path / "run")
    state = LongRunState(goal_run_id="g-1", computer_id="c-1")
    store.write(state)
    restored = LongRunningHarness.resume(store, expected_goal_run_id="g-1")
    assert restored.state.goal_run_id == "g-1"


def test_harness_resume_foreign_lineage_blocked(tmp_path):
    from runtime.execution.long_running import (
        HarnessError,
        LongRunCheckpointStore,
        LongRunningHarness,
        LongRunState,
    )

    store = LongRunCheckpointStore(tmp_path / "run")
    store.write(LongRunState(goal_run_id="g-1", computer_id="c-1"))
    with pytest.raises(HarnessError, match="blocked"):
        LongRunningHarness.resume(store, expected_goal_run_id="g-2")
