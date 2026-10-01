"""Comprehensive regression test suite for Executor Qualification Gate (P7).

Covers all 17 canonical conditions:
1. AGY > CODEX > HICODE > PI > GROK preference
2. explicit pin 不被替换
3. unhealthy AGY -> CODEX
4. unhealthy AGY+CODEX -> HICODE
5. capability incompatible executor 不参与候选
6. substitution 有 evidence
7. heartbeat healthy + provider dead => 不得 HEALTHY
8. timeout => WORKER_TIMEOUT
9. SIGKILL => WORKER_CRASH
10. broken proxy => TRANSPORT_FAILURE
11. cancel => WORKER_CANCELLED
12. submodule failure => SUBMODULE_FAILURE
13. zombie/reap failure => qualification FAIL
14. false success => qualification FAIL
15. 5-way parallel isolation
16. LEVEL_1 与 LEVEL_2 不得混淆
17. LIVE_BLOCKED_EXTERNAL 不得伪装 LIVE_QUALIFIED
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
from pathlib import Path

import pytest

from runtime.coding.worktree import WorktreeManager, teardown_worktree
from scripts.qualify_executors import (
    QualificationGateResult,
    run_deterministic_qualification,
)
from veya.remote.executor_health import (
    registry_order,
    ExecutorFailureClass,
    ExecutorHealth,
    ExecutorHealthRegistry,
    SubstitutionEvidence,
    classify_executor_failure,
    resolve_executor,
)
from veya.remote.worker_runtime import FinishBoundary


def test_01_default_preference_order():
    """1. AGY > OPENCODE > CLAUDE_CODE > PI > GROK > DSH > CODEX preference."""
    cand, sub = resolve_executor()
    assert cand == "antigravity"
    assert sub is None
    assert registry_order() == (
        "antigravity",
        "opencode",
        "claude_code",
        "pi",
        "grok",
        "dsh",
        "codex",
    )


def test_02_explicit_pin_not_substituted():
    """2. explicit pin 不被替换."""
    reg = ExecutorHealthRegistry()
    reg.record_failure("codex", ExecutorFailureClass.PROVIDER_UNAVAILABLE)
    # Even though codex is UNAVAILABLE, explicit_pin preserves it
    selected, sub = resolve_executor(requested="codex", explicit_pin=True, health_registry=reg)
    assert selected == "codex"
    assert sub is None

    selected_agy, sub_agy = resolve_executor(
        requested="antigravity", explicit_pin=True, health_registry=reg
    )
    assert selected_agy == "antigravity"
    assert sub_agy is None


def test_03_unhealthy_agy_fallback_to_opencode():
    """3. unhealthy AGY -> OPENCODE."""
    reg = ExecutorHealthRegistry()
    reg.record_failure("antigravity", ExecutorFailureClass.PROVIDER_UNAVAILABLE)
    assert reg.get_health("antigravity") == ExecutorHealth.UNAVAILABLE

    selected, sub = resolve_executor(
        requested="antigravity", explicit_pin=False, health_registry=reg
    )
    assert selected == "opencode"
    assert sub is not None
    assert sub.requested_executor == "antigravity"
    assert sub.selected_executor == "opencode"
    assert "antigravity is UNAVAILABLE" in sub.substitution_reason


def test_04_unhealthy_agy_and_opencode_fallback_to_claude_code():
    """4. unhealthy AGY+OPENCODE -> CLAUDE_CODE."""
    reg = ExecutorHealthRegistry()
    reg.record_failure("antigravity", ExecutorFailureClass.PROVIDER_UNAVAILABLE)
    reg.record_failure("opencode", ExecutorFailureClass.TRANSPORT_FAILURE)
    assert reg.get_health("antigravity") == ExecutorHealth.UNAVAILABLE
    assert reg.get_health("opencode") == ExecutorHealth.UNAVAILABLE

    selected, sub = resolve_executor(
        requested="antigravity", explicit_pin=False, health_registry=reg
    )
    assert selected == "claude_code"
    assert sub is not None
    assert sub.selected_executor == "claude_code"


def test_04b_quota_exhausted_codex_auto_skipped():
    """CODEX 遇 quota exhausted (429 / purchase credits) 自动跳过."""
    reg = ExecutorHealthRegistry()
    # If AGY, OPENCODE, CLAUDE_CODE, PI, GROK, DSH are down:
    for w in ("antigravity", "opencode", "claude_code", "pi", "grok", "dsh"):
        reg.record_failure(w, ExecutorFailureClass.PROVIDER_UNAVAILABLE)
    # Simulate CODEX quota exhausted:
    reg.record_failure(
        "codex", ExecutorFailureClass.PROVIDER_UNAVAILABLE, detail="429 quota exhausted"
    )
    # Every capable executor is down: routing must fail closed, never fall back
    # to a retired executor (hicode).
    with pytest.raises(ValueError):
        resolve_executor(health_registry=reg)


def test_05_capability_incompatible_excluded():
    """5. capability incompatible executor 不参与候选."""
    reg = ExecutorHealthRegistry()
    # Require persistent context (supported by pi, grok, dsh; NOT antigravity, opencode, codex, hicode)
    selected, _ = resolve_executor(
        required_capabilities={"supports_persistent_context": True},
        health_registry=reg,
    )
    # Pi is the first preference among compatible executors (pi > grok > dsh)
    assert selected == "pi"


def test_06_substitution_has_evidence():
    """6. substitution 有 evidence."""
    reg = ExecutorHealthRegistry()
    reg.record_failure("antigravity", ExecutorFailureClass.AUTH_FAILURE)
    selected, sub = resolve_executor(requested="antigravity", health_registry=reg)
    assert selected == "opencode"
    assert isinstance(sub, SubstitutionEvidence)
    evidence_dict = sub.to_dict()
    assert evidence_dict["kind"] == "executor_substitution"
    assert evidence_dict["requested_executor"] == "antigravity"
    assert evidence_dict["selected_executor"] == "opencode"
    assert "substitution_reason" in evidence_dict
    assert "health_snapshot" in evidence_dict
    assert evidence_dict["health_snapshot"]["antigravity"] == "UNAVAILABLE"


def test_07_heartbeat_healthy_provider_dead_not_healthy():
    """7. heartbeat healthy + provider dead => 不得 HEALTHY."""
    reg = ExecutorHealthRegistry()
    reg.set_provider_status("codex", reachable=False, detail="502 Bad Gateway")
    # Heartbeat comes in indicating process alive
    reg.set_heartbeat("codex", alive=True)

    # Invariant: process alive does NOT make dead provider HEALTHY
    health = reg.get_health("codex")
    assert health != ExecutorHealth.HEALTHY
    assert health == ExecutorHealth.UNAVAILABLE


def test_08_timeout_classified_as_worker_timeout():
    """8. timeout => WORKER_TIMEOUT."""
    fc1 = classify_executor_failure(status="TIMEOUT")
    assert fc1 == ExecutorFailureClass.WORKER_TIMEOUT

    fc2 = classify_executor_failure(error=TimeoutError("inactivity timeout expired"))
    assert fc2 == ExecutorFailureClass.WORKER_TIMEOUT


def test_09_sigkill_classified_as_worker_crash():
    """9. SIGKILL => WORKER_CRASH."""
    fc1 = classify_executor_failure(exit_code=-signal.SIGKILL)
    assert fc1 == ExecutorFailureClass.WORKER_CRASH

    fc2 = classify_executor_failure(exit_code=137)
    assert fc2 == ExecutorFailureClass.WORKER_CRASH


def test_10_broken_proxy_classified_as_transport_failure():
    """10. broken proxy => TRANSPORT_FAILURE."""
    fc = classify_executor_failure(detail="proxy connection closed: ECONNREFUSED 127.0.0.1:7890")
    assert fc == ExecutorFailureClass.TRANSPORT_FAILURE


def test_11_cancel_classified_as_worker_cancelled():
    """11. cancel => WORKER_CANCELLED."""
    fc1 = classify_executor_failure(status="CANCELLED")
    assert fc1 == ExecutorFailureClass.WORKER_CANCELLED

    fc2 = classify_executor_failure(error=asyncio.CancelledError())
    assert fc2 == ExecutorFailureClass.WORKER_CANCELLED


def test_12_submodule_failure_classified():
    """12. submodule failure => SUBMODULE_FAILURE."""
    fc = classify_executor_failure(
        detail="submodule provisioning failed: fatal: reference is not a tree"
    )
    assert fc == ExecutorFailureClass.SUBMODULE_FAILURE


def test_13_zombie_reap_failure_fails_qualification():
    """13. zombie/reap failure => qualification FAIL."""
    fc = classify_executor_failure(detail="zombie process detected: leftover process PID 1234")
    assert fc == ExecutorFailureClass.PROCESS_REAP_FAILURE

    # In qualification result, zombie_process > 0 blocks qualification
    res = QualificationGateResult(base_sha="test", final_sha="test")
    res.zombie_process = 1
    ok = run_deterministic_qualification(res)
    assert ok is False
    assert res.deterministic_qualified is False


def test_14_false_success_fails_qualification():
    """14. false success => qualification FAIL."""
    # FinishBoundary blocks premature completion
    fb = FinishBoundary(worker_final_claim=True, active_process_count=1)
    assert fb.can_complete() is False

    # In qualification result, false_success > 0 blocks qualification
    res = QualificationGateResult(base_sha="test", final_sha="test")
    res.false_success = 1
    ok = run_deterministic_qualification(res)
    assert ok is False
    assert res.deterministic_qualified is False


def test_15_5way_parallel_isolation(tmp_path: Path):
    """15. 5-way parallel isolation."""
    repo_path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_path)], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "t"], check=True)
    (repo_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "init"], check=True)

    wm = WorktreeManager(repo_path)
    lanes = ["lane_agy", "lane_codex", "lane_hicode", "lane_pi", "lane_grok"]
    records = []
    for lane in lanes:
        rec = wm.create(f"task-{lane}", f"task objective for {lane}")
        records.append(rec)
        p = Path(rec.path)
        (p / f"{lane}.txt").write_text(f"content-{lane}\n", encoding="utf-8")

    # Verify complete isolation: each lane file exists ONLY in its own worktree
    for i, lane in enumerate(lanes):
        target_p = Path(records[i].path)
        assert (target_p / f"{lane}.txt").exists()
        for j, other_lane in enumerate(lanes):
            if i != j:
                assert not (Path(records[j].path) / f"{lane}.txt").exists(), (
                    f"Cross-contamination: {lane} found in {other_lane}"
                )

    # Teardown all
    for rec in records:
        teardown_worktree(Path(rec.path), execution_status="COMPLETED")


def test_16_level1_and_level2_not_conflated():
    """16. LEVEL_1 与 LEVEL_2 不得混淆."""
    res = QualificationGateResult(base_sha="test", final_sha="test")
    ok = run_deterministic_qualification(res)
    assert ok is True
    assert res.deterministic_qualified is True
    # Deterministic PASS must NEVER claim live_qualified!
    assert res.live_qualified is False


def test_17_live_blocked_external_not_disguised_as_live_qualified():
    """17. LIVE_BLOCKED_EXTERNAL 不得伪装 LIVE_QUALIFIED."""
    res = QualificationGateResult(
        base_sha="test",
        final_sha="test",
        deterministic_qualified=True,
        live_qualified=False,
        live_blocked_external=True,
    )
    assert res.live_qualified is False
    assert res.live_blocked_external is True
