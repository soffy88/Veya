"""Pytest entry point for the P1 qualification harness.

- ``test_no_fake_sleep``: static guard — no sleep-based padding may exist in
  the harness package (fast, always runs).
- ``test_p1_qualification_full`` (marked slow): executes the real runner as a
  subprocess.  Clock is controlled by ``P1Q_TARGET_SECONDS`` (default 1800 s
  = 30 min floor).  Smoke self-checks use a short target; the post-I4
  qualification run uses the default.

Gates are reported separately and never merged into one "overall PASS":

- MECHANICS_RESULT (HARNESS_MECHANICS, 7 controlled scenarios + no-sleep)
- DURATION_GATE_RESULT (wall clock inside [1800, 3600] s)
- FORMAL mode: canonical production-seam entry with REAL_* counters.

CONTROLLED_PROVIDER_FAILOVER qualifies the adapter path only;
REAL_PROVIDER_FAILOVER stays NOT_RUN until the post-I4 canonical-path run.
This test never declares qualification PASS (see evals/p1_qualification/README.md).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_PKG = REPO_ROOT / "evals" / "p1_qualification"


def test_no_fake_sleep():
    offenders = []
    for path in sorted(HARNESS_PKG.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "time.sleep(" in stripped or "asyncio.sleep(" in stripped:
                offenders.append(f"{path.name}:{i}: {stripped}")
    assert not offenders, f"fake sleep found in harness: {offenders}"


def test_harness_package_layout():
    for name in (
        "workload.py",
        "collector.py",
        "scenarios.py",
        "assertions.py",
        "report.py",
        "run_qualification.py",
        "canonical_run.py",
        "formal_run.py",
        "formal_checks.py",
        "invariants.py",
    ):
        assert (HARNESS_PKG / name).is_file(), f"missing {name}"


@pytest.mark.slow
def test_p1_qualification_full(tmp_path):
    target = float(os.environ.get("P1Q_TARGET_SECONDS", "1800"))
    run_root = tmp_path / "p1q-run"
    cmd = [
        sys.executable,
        "evals/p1_qualification/run_qualification.py",
        "--mode",
        "smoke",
        "--target-seconds",
        str(target),
        "--work-root",
        str(run_root),
    ]
    proc = subprocess.run(
        cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=target + 900
    )
    assert (run_root / "events.jsonl").is_file(), (
        f"no event stream preserved. stdout={proc.stdout[-2000:]} stderr={proc.stderr[-2000:]}"
    )
    assert (run_root / "report.json").is_file(), "no final report generated"
    report = json.loads((run_root / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "HARNESS_COMPLETED"
    assert report["qualification_class"] == "HARNESS_MECHANICS"
    assert report["real_work_only"] is True and report["fake_sleep_used"] is False
    # CONTROLLED vs REAL semantics must be explicit in every report.
    assert report["real_provider_failover"] == "NOT_RUN"
    final_real = report["final_real_qualification"]
    assert final_real["status"] == "NOT_RUN"
    for key in (
        "real_provider_calls",
        "real_tool_executions",
        "real_context_cycles",
        "real_restarts",
        "real_checkpoints",
    ):
        assert final_real[key] == "NOT_RUN", f"final REAL_* leak: {key}"
    assert report["elapsed_s"] >= min(target, 3600) - 5, (
        f"duration not built from real work: {report['elapsed_s']}s < {target}s"
    )
    mechanics = [c for c in report["checks"] if c["name"] != "duration_in_30_60min_real_work"]
    duration_gate = [c for c in report["checks"] if c["name"] == "duration_in_30_60min_real_work"]
    assert len(mechanics) == 7 and len(duration_gate) == 1
    mechanics_result = "PASS" if all(c["passed"] for c in mechanics) else "FAIL"
    duration_result = "PASS" if all(c["passed"] for c in duration_gate) else "FAIL"
    print(
        f"\nMECHANICS_RESULT={mechanics_result} SCENARIOS={sum(1 for c in mechanics if c['passed'])}/7"
    )
    print(f"DURATION_GATE_RESULT={duration_result} elapsed={report['elapsed_s']}s")
    # Controlled failover is mechanics; a controlled TimeoutError must never
    # be reported as a real external provider outage qualification.
    provider_check = next(c for c in mechanics if c["name"].startswith("provider_failover"))
    assert "controlled" in provider_check["detail"], "failover class must be labeled controlled"
    assert report["controlled_provider_failover"] == (
        "PASS" if provider_check["passed"] else "FAIL"
    )
    # MECHANICS must be green in every mode; the duration gate is allowed to
    # miss ONLY in short-clock smoke (floor is fixed at 1800 s).
    assert mechanics_result == "PASS", f"mechanics red: {[c for c in mechanics if not c['passed']]}"
    scenarios_ok = sum(1 for c in mechanics if c["passed"])
    assert scenarios_ok == 7, f"SCENARIOS={scenarios_ok}/7, need 7/7"
    if target < 1800:
        assert duration_result == "FAIL", "smoke must not satisfy the 1800 s floor"
        assert proc.returncode == 2, f"smoke must exit 2, got {proc.returncode}"
        return
    assert duration_result == "PASS", f"duration gate red: {duration_gate}"
    assert proc.returncode == 0, f"runner exit={proc.returncode} stderr={proc.stderr[-2000:]}"
    assert report["checks_passed"] == report["checks_total"] == 8


@pytest.mark.slow
def test_p1_formal_entry_short(tmp_path):
    """Short formal probe: canonical entry + seam actions + REAL_* wiring.

    Does NOT satisfy the 1800 s duration gate (short clock by design); it
    proves the formal machinery traverses the production seam end to end.
    """
    target = float(os.environ.get("P1Q_FORMAL_TARGET", "90"))
    run_root = tmp_path / "p1q-formal"
    cmd = [
        sys.executable,
        "evals/p1_qualification/run_qualification.py",
        "--mode",
        "formal",
        "--target-seconds",
        str(target),
        "--work-root",
        str(run_root),
    ]
    proc = subprocess.run(
        cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=target + 900
    )
    assert (run_root / "report.json").is_file(), (
        f"no formal report. stdout={proc.stdout[-2000:]} stderr={proc.stderr[-3000:]}"
    )
    report = json.loads((run_root / "report.json").read_text(encoding="utf-8"))
    assert report["qualification_class"] == "FORMAL_CANONICAL_RUN"
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["canonical_production_entry"]["passed"], by_name["canonical_production_entry"]
    assert report["real_tool_executions"] >= 5, report["real_tool_executions"]
    assert report["real_context_cycles"] >= 5
    assert report["real_checkpoints"] >= 1
    assert report["real_provider_failover"] == "NOT_RUN"
    # Short clock: duration gate must miss; entry must hold regardless.
    assert not by_name["duration_in_30_60min_real_work"]["passed"]
    print(f"\nFORMAL_ENTRY=PASS actions={report['real_tool_executions']}")
