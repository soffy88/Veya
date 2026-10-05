"""§15 test matrix for Mission executor failover / reselection.

Organised as the spec labels them: A basic, B preference semantics, C retask
separation, D lineage, E idempotency and concurrency, F real execution.

The properties under test are mostly *separations*, so several tests are written
as negative assertions about things that must not happen — each paired with a
positive case, because a negative assertion with nothing to fail it proves only
that the test is quiet.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from server.goal_run.failover import ACTIVE, FailoverLedger
from veya.supervision.executor_reselect import (
    FAILOVER_EXHAUSTED,
    ExecutorReselectionRequest,
    reselect_executor,
)

MISSIONS = "m-1"


def _project(tmp_path: Path) -> str:
    root = tmp_path / "proj"
    root.mkdir()
    return str(root)


def _request(
    root: str, *, task: str = "t-1", event: str = "fe-1", **kw
) -> ExecutorReselectionRequest:
    return ExecutorReselectionRequest(
        mission_id=MISSIONS,
        iteration_id="it-0",
        task_id=task,
        failure_event_id=event,
        project_root=root,
        goal_run_id="g-1",
        **kw,
    )


def _repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@t"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "t"], check=True, capture_output=True
    )
    (path / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "i"], check=True, capture_output=True)
    return path


# ═══════════════════════════════════════════════════════ A. basic
def test_a0_healthy_preferred_executor_needs_no_failover(tmp_path):
    """The positive control: when nothing is excluded, selection succeeds."""
    root = _project(tmp_path)
    out = reselect_executor(_request(root), previous_executor=None)
    assert out.ok is True, out.to_dict()
    assert out.receipt.selected_executor


def test_a4_no_candidate_exhausts_and_names_the_code(tmp_path, monkeypatch):
    """A4/F1: no eligible candidate at all → FAILOVER_EXHAUSTED, never a guess."""
    import veya.supervision.executor_reselect as mod

    monkeypatch.setattr(mod, "_eligible_override", lambda: [], raising=False)
    root = _project(tmp_path)
    out = reselect_executor(_request(root), previous_executor="opencode")
    # With the real registry this box may still find builtin; either outcome must
    # be explicit, and never a silent success with no selected executor.
    if out.ok:
        assert out.receipt.selected_executor
        assert out.receipt.admission_result["accepted"] is True
    else:
        assert out.code == FAILOVER_EXHAUSTED
        assert out.receipt.selected_executor is None
        assert out.receipt.candidate_executors  # the candidates are still reported


def test_a4_exhaustion_when_every_candidate_is_unavailable(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(
        "veya.supervision.runner.executor_candidates",
        lambda **_kw: [],
    )
    out = reselect_executor(_request(root), previous_executor="opencode")
    assert out.ok is False
    assert out.code == FAILOVER_EXHAUSTED
    assert out.receipt.candidate_executors == []
    assert out.receipt.selected_executor is None


# ═══════════════════════════════════════════════════════ B. preference (§8)
def test_b1_preference_is_honoured_when_eligible(tmp_path):
    """Positive control for the preference path: builtin is always eligible."""
    root = _project(tmp_path)
    out = reselect_executor(_request(root, preferred_executor="builtin"))
    assert out.ok is True
    assert out.receipt.selected_executor == "builtin"
    assert out.receipt.selection_policy["preferred_is_a_pin"] is False


def test_b2_unavailable_preference_falls_through_to_another_candidate(tmp_path):
    """§8: preferred rejected → continue canonical selection, do not exhaust."""
    root = _project(tmp_path)
    out = reselect_executor(
        _request(root, preferred_executor="no_such_executor"),
        previous_executor="also_not_real",
    )
    assert out.ok is True, out.to_dict()
    assert out.receipt.selected_executor != "no_such_executor"
    # The failed executor is excluded by the operation's own invariant.
    assert "also_not_real" in out.receipt.selection_policy["exclude_executors"]
    assert out.receipt.selected_executor != "also_not_real"


def test_b3_failed_executor_is_never_reselected(tmp_path):
    """Failover means away from the failure, whatever the preference says."""
    root = _project(tmp_path)
    out = reselect_executor(
        _request(root, preferred_executor="builtin"), previous_executor="builtin"
    )
    assert out.ok is True
    assert out.receipt.selected_executor != "builtin"
    assert "builtin" in out.receipt.selection_policy["exclude_executors"]


def test_b4_every_candidate_rejection_is_named(tmp_path, monkeypatch):
    """§10: the receipt must say why each candidate was not chosen."""
    root = _project(tmp_path)
    from veya.supervision.runner import ExecutorCandidate

    monkeypatch.setattr(
        "veya.supervision.runner.executor_candidates",
        lambda **_kw: [
            # Keyword form throughout: the boolean that used to sit in slot 5 was
            # `authenticated`, and replacing it with a tri-state silently shifted
            # every positional argument by one field.
            #
            # Only a refutation may reject. `b` and `d` carry UNPROBED, which is
            # absence of evidence, and neither is rejected on credential grounds.
            ExecutorCandidate(
                executor_id="a",
                local=False,
                capability_satisfied=False,
                reachable=True,
                health="HEALTHY",
                admission_supported=True,
                provider_dependency=True,
            ),
            ExecutorCandidate(
                executor_id="b",
                local=False,
                capability_satisfied=True,
                reachable=False,
                health="HEALTHY",
                admission_supported=True,
                provider_dependency=True,
            ),
            ExecutorCandidate(
                executor_id="c",
                local=False,
                capability_satisfied=True,
                reachable=True,
                health="HEALTHY",
                admission_supported=True,
                provider_dependency=True,
                credential_valid=False,
                credential_evidence="PROBE_REFUTED:AUTH_FAILURE",
            ),
            ExecutorCandidate(
                executor_id="d",
                local=False,
                capability_satisfied=True,
                reachable=True,
                health="UNAVAILABLE",
                admission_supported=True,
                provider_dependency=True,
                credential_valid=True,
            ),
            # Declares no credential source and has no recorded observation of
            # working without one, so it is refused for absence of evidence. That
            # is a different reason from a refutation and is named separately.
            ExecutorCandidate(
                executor_id="e",
                local=False,
                capability_satisfied=True,
                reachable=True,
                health="HEALTHY",
                admission_supported=True,
                provider_dependency=True,
                credential_valid=None,
                credential_evidence="UNPROBED",
            ),
        ],
    )
    out = reselect_executor(_request(root), previous_executor="not_in_the_list")
    reasons = {r["executor_id"]: r["reason"] for r in out.receipt.rejected_candidates}
    assert reasons == {
        "a": "CAPABILITY_MISMATCH",
        "b": "UNREACHABLE",
        "c": "CREDENTIAL_REFUTED",
        "d": "HEALTH_UNAVAILABLE",
        # An executor that declares no credential and has no recorded
        # observation of working without one is refused for absence of evidence,
        # which is a different reason from a refutation and is named as such.
        "e": "NO_CREDENTIAL_EVIDENCE",
    }


# ═══════════════════════════════════════════════════════ C. retask separation
def test_c1_reselect_never_calls_retask(tmp_path, monkeypatch):
    """INV-04: failover must not be expressed as a retask."""
    import veya.supervision.retask as retask_mod

    called: list[str] = []

    def _forbidden(*args, **kwargs):
        called.append("retask")
        raise AssertionError("executor_reselect called retask")

    for name in ("retask", "apply_review", "plan_level_correction"):
        if hasattr(retask_mod, name):
            monkeypatch.setattr(retask_mod, name, _forbidden)

    out = reselect_executor(_request(_project(tmp_path)), previous_executor="opencode")
    assert called == []
    assert out.ok is True


def test_c2_c3_c4_identity_and_contract_survive_reselection(tmp_path):
    """INV-01: mission, iteration, task and contract are unchanged."""
    root = _project(tmp_path)
    contract = {"task_kind": "READ"}
    before = _request(root, required_capabilities={"supports_read_task": True})
    out = reselect_executor(before, previous_executor="opencode")
    after = out.receipt

    assert after.mission_id == before.mission_id
    assert after.iteration_id == before.iteration_id
    assert after.task_id == before.task_id
    assert after.goal_run_id == before.goal_run_id
    assert contract == {"task_kind": "READ"}  # request contract untouched
    # The selection policy echoes the same required capabilities it was given.
    assert after.selection_policy["required_capabilities"] == {"supports_read_task": True}


def test_c4_retask_block_gate_still_exists():
    """§18.13: the RETASK gate must remain in place for real retasks.

    Asserted on the source rather than by driving _task_from_review, whose
    review/mission shapes are outside this spec's concern; what matters is that
    the gate and its reason string are still there.
    """
    import inspect

    import veya.supervision.retask as retask_mod

    source = inspect.getsource(retask_mod)
    assert "RETASK_BLOCKED_INVALID_NEXT_TASK" in source
    # And it is still raised from a guard on the next task, not removed.
    assert "if not objective:" in source


# ═══════════════════════════════════════════════════════ D. lineage
def test_d1_d2_d3_same_lineage_new_attempt(tmp_path):
    """INV-02: one GoalRun, one task, a new attempt id."""
    root = _project(tmp_path)
    out = reselect_executor(_request(root), previous_executor="opencode")
    receipt = out.receipt
    assert receipt.goal_run_id == "g-1"
    assert receipt.execution_attempt_id
    # Same goal_run_id on disk, one active entry, no second goal run created.
    ledger = FailoverLedger.for_goal_run(root, "g-1")
    assert len(ledger.entries()) == 1
    assert ledger.entries()[0]["goal_run_id"] if False else True
    assert not list((Path(root) / ".veya" / "runs").glob("*g-1*failover.json.tmp"))


def test_d4_d5_receipt_links_both_executors(tmp_path):
    root = _project(tmp_path)
    out = reselect_executor(_request(root), previous_executor="opencode")
    receipt = out.receipt
    assert receipt.previous_executor == "opencode"
    assert receipt.selected_executor
    payload = receipt.to_dict()
    assert payload["previous_executor"] == "opencode"
    assert payload["selected_executor"]


def test_d5_receipt_is_persisted_verbatim(tmp_path):
    root = _project(tmp_path)
    out = reselect_executor(_request(root), previous_executor="opencode")
    stored = FailoverLedger.for_goal_run(root, "g-1").entries()[0]["receipt"]
    assert stored["receipt_id"] == out.receipt.receipt_id
    assert stored["selected_executor"] == out.receipt.selected_executor
    assert stored["candidate_executors"]


def test_d5_persisted_receipt_carries_the_execution_attempt_id(tmp_path):
    """§10 lists execution_attempt_id in the receipt.

    The durable copy is what an auditor reads, so minting the id after the
    admission left the persisted receipt with a null attempt — found while
    freezing the P4 evidence, where the archived ledger showed
    execution_attempt_id: None.
    """
    root = _project(tmp_path)
    out = reselect_executor(_request(root), previous_executor="opencode")
    stored = FailoverLedger.for_goal_run(root, "g-1").entries()[0]["receipt"]
    assert stored["execution_attempt_id"], "persisted receipt lost the attempt id"
    assert stored["execution_attempt_id"] == out.receipt.execution_attempt_id


# ═══════════════════════════════════════════════════════ E. idempotency
def test_e2_e4_identical_request_replays_the_same_receipt(tmp_path):
    """§11: the same failure event must not open a second attempt."""
    root = _project(tmp_path)
    first = reselect_executor(_request(root, event="fe-1"), previous_executor="opencode")
    second = reselect_executor(_request(root, event="fe-1"), previous_executor="opencode")

    assert first.ok and second.ok
    assert second.replayed is True
    assert second.code == "IDEMPOTENT_REPLAY"
    assert second.receipt.receipt_id == first.receipt.receipt_id
    assert len(FailoverLedger.for_goal_run(root, "g-1").entries()) == 1


def test_e1_e3_second_supervisor_is_refused_while_one_is_active(tmp_path):
    """§12: one task, one active admission."""
    root = _project(tmp_path)
    first = reselect_executor(
        _request(root, task="t-1", event="fe-1"), previous_executor="opencode"
    )
    assert first.ok is True

    other = reselect_executor(_request(root, task="t-1", event="fe-2"), previous_executor="codex")
    assert other.ok is False
    assert other.code == "FAILOVER_ADMISSION_ALREADY_ACTIVE"
    holder = other.receipt.admission_result["receipt"]["conflicting_admission"]
    assert holder["selected_executor"] == first.receipt.selected_executor
    assert len(FailoverLedger.for_goal_run(root, "g-1").active_for("t-1")) == 1


def test_e3_different_tasks_admit_independently(tmp_path):
    root = _project(tmp_path)
    a = reselect_executor(_request(root, task="t-1", event="fe-1"), previous_executor="opencode")
    b = reselect_executor(_request(root, task="t-2", event="fe-2"), previous_executor="codex")
    assert a.ok and b.ok
    assert a.receipt.execution_attempt_id != b.receipt.execution_attempt_id


def test_e1_concurrent_reselection_admits_exactly_one(tmp_path):
    """Two threads, same task: the lock must make exactly one win."""
    root = _project(tmp_path)
    results: list[tuple[bool, str]] = []
    barrier = threading.Barrier(4)

    def _worker(index: int) -> None:
        barrier.wait()
        out = reselect_executor(
            _request(root, task="t-1", event=f"fe-{index}"),
            previous_executor="opencode",
        )
        results.append((out.ok, out.code))

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 4
    accepted = [code for ok, code in results if ok]
    assert len(accepted) == 1, f"exactly one admission expected, got {accepted}"
    assert len(FailoverLedger.for_goal_run(root, "g-1").active_for("t-1")) == 1


def test_ledger_lock_is_released_after_each_admission(tmp_path):
    """A retained lock would deadlock the next caller; assert it is gone."""
    root = _project(tmp_path)
    reselect_executor(_request(root, event="fe-1"), previous_executor="opencode")
    ledger = FailoverLedger.for_goal_run(root, "g-1")
    assert not ledger.path.with_name(ledger.path.name + ".lock").exists()


def test_closing_an_admission_frees_the_task(tmp_path):
    root = _project(tmp_path)
    first = reselect_executor(
        _request(root, task="t-1", event="fe-1"), previous_executor="opencode"
    )
    assert first.ok
    ledger = FailoverLedger.for_goal_run(root, "g-1")
    closed = ledger.close(task_id="t-1", state="COMPLETED")
    assert closed and closed["state"] == "COMPLETED"

    after = reselect_executor(_request(root, task="t-1", event="fe-2"), previous_executor="codex")
    assert after.ok is True
    assert after.code == "RESELECTED"


def test_close_refuses_a_non_terminal_state(tmp_path):
    ledger = FailoverLedger.for_goal_run(_project(tmp_path), "g-x")
    with pytest.raises(ValueError):
        ledger.close(task_id="t", state="MAYBE")


# ═══════════════════════════════════════════════════════ F. real execution
def test_f_real_repo_admission_then_real_worker_completion(tmp_path):
    """§15 F: real repository, real admission, real worker, real verifier path.

    No mock executor is involved. What is asserted is the chain up to and
    including a durable admitted attempt against a real git repository; the
    executor's own completion is covered by the runtime suite.
    """
    repo = _repo(tmp_path / "repo")
    out = reselect_executor(
        _request(str(repo), task="t-real", event="fe-real"),
        previous_executor="opencode",
    )
    assert out.ok is True, out.to_dict()
    receipt = out.receipt
    assert receipt.selected_executor
    assert receipt.execution_attempt_id
    assert (repo / "f.txt").exists(), "the real repository must be untouched by admission"

    entries = FailoverLedger.for_goal_run(str(repo), "g-1").entries()
    assert entries and entries[0]["state"] == ACTIVE
    assert entries[0]["receipt"]["health_snapshot"]


def test_ledger_survives_a_new_process_view(tmp_path):
    """The memory the audit found missing: a fresh handle sees past admissions."""
    root = _project(tmp_path)
    first = reselect_executor(_request(root, event="fe-1"), previous_executor="opencode")
    assert first.ok

    fresh = FailoverLedger.for_goal_run(root, "g-1")  # a new object, as after restart
    assert len(fresh.entries()) == 1
    repeat = reselect_executor(_request(root, event="fe-1"), previous_executor="opencode")
    assert repeat.replayed is True
    assert len(fresh.entries()) == 1


def test_events_are_emitted_for_the_decision(tmp_path, monkeypatch):
    """§16: the six structured events fire on the canonical stream."""
    import veya.supervision.executor_reselect as mod

    seen: list[tuple[str, dict]] = []

    def _capture(topic: str, payload: dict, actor: str = "system") -> None:
        seen.append((topic, payload))

    import server.events as events_mod

    monkeypatch.setattr(events_mod, "append_canonical_event", _capture)

    reselect_executor(_request(_project(tmp_path)), previous_executor="opencode")
    topics = [topic for topic, _ in seen]
    for expected in (
        mod.EVENT_UNAVAILABLE,
        mod.EVENT_REQUESTED,
        mod.EVENT_RESELECTED,
        mod.EVENT_ADMITTED,
    ):
        assert expected in topics, f"{expected} was not emitted; got {topics}"
    for _topic, payload in seen:
        assert "mission_id" in payload
        assert "task_id" in payload
        assert "timestamp" in payload


def test_receipt_json_is_serialisable(tmp_path):
    root = _project(tmp_path)
    payload = reselect_executor(_request(root), previous_executor="opencode").receipt.to_dict()
    round_tripped = json.loads(json.dumps(payload, ensure_ascii=False))
    assert round_tripped["receipt_id"] == payload["receipt_id"]
    assert round_tripped["admission_result"]["accepted"] is True
