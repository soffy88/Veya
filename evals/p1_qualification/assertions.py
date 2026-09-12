"""Acceptance assertions for the P1 qualification harness.

Two gates, never merged into one "overall PASS":

- MECHANICS (HARNESS_MECHANICS): the 7 controlled scenario checks below.
  Controlled faults qualify adapter/replan/ledger/restart paths only.
- DURATION_GATE: wall clock inside [1800, 3600] s built from real cycles.

FINAL_REAL_QUALIFICATION (real provider outage, real 30-60 min canonical
production path) is a separate verdict and is NOT_RUN by this harness.
"""

from __future__ import annotations

TARGET_MIN_S = 1800  # 30 min lower bound
TARGET_MAX_S = 3600  # 60 min upper bound

DURATION_CHECK_NAME = "duration_in_30_60min_real_work"

MECHANICS_CHECK_NAMES = (
    "context_pressure_compaction_no_drift",
    "provider_failover_same_goalrun_no_semantic_drift",
    "tool_failure_replan_recovery",
    "supervisor_restart_same_computer_goalrun",
    "side_effect_no_duplicates",
    "verification_convergence_fail_then_pass",
    "checkpoint_resume_latest_valid",
)


def check_duration(elapsed_s: float, target_s: float) -> tuple:
    lo = max(TARGET_MIN_S, min(target_s, TARGET_MAX_S))
    ok = lo <= elapsed_s <= TARGET_MAX_S + 300
    return (
        "duration_in_30_60min_real_work",
        ok,
        f"elapsed={elapsed_s:.1f}s target_floor={lo:.0f}s band=[1800,3600]",
    )


def check_context(res: dict) -> tuple:
    ok = (
        bool(res.get("pressure_hit"))
        and res.get("context_drift") == 0
        and res.get("tokens_after", 0) < res.get("tokens_before", 0)
    )
    return (
        "context_pressure_compaction_no_drift",
        ok,
        f"pressure_hit={res.get('pressure_hit')} drift={res.get('context_drift')} "
        f"tokens {res.get('tokens_before')}->{res.get('tokens_after')}",
    )


def check_provider(res: dict) -> tuple:
    ok = (
        res.get("failover_class") == "controlled"
        and bool(res.get("failover"))
        and bool(res.get("same_goalrun"))
        and bool(res.get("schema_ok"))
        and res.get("semantic_drift") == 0
    )
    return (
        "provider_failover_same_goalrun_no_semantic_drift",
        ok,
        f"class={res.get('failover_class')} used={res.get('used')} "
        f"calls={res.get('calls')} drift={res.get('semantic_drift')}",
    )


def check_replan(res: dict) -> tuple:
    ok = (
        bool(res.get("failure_preserved"))
        and res.get("replans", 0) >= 1
        and bool(res.get("progressed"))
        and bool(res.get("recovered"))
    )
    return (
        "tool_failure_replan_recovery",
        ok,
        f"failure_preserved={res.get('failure_preserved')} replans={res.get('replans')} "
        f"progressed={res.get('progressed')}",
    )


def check_restart(res: dict) -> tuple:
    ok = (
        bool(res.get("same_computer"))
        and bool(res.get("same_goalrun"))
        and bool(res.get("continued"))
    )
    return (
        "supervisor_restart_same_computer_goalrun",
        ok,
        f"computer={res.get('computer_id')} continued={res.get('continued')}",
    )


def check_side_effect(res: dict, metrics: dict) -> tuple:
    dup = int(res.get("duplicates", -1))
    ok = dup == 0 and bool(res.get("resumed_equal")) and res.get("invocations") == 1
    return (
        "side_effect_no_duplicates",
        ok,
        f"invocations={res.get('invocations')} duplicates={dup} resumed_equal={res.get('resumed_equal')}",
    )


def check_verification(res: dict) -> tuple:
    ok = (
        bool(res.get("failed_once"))
        and res.get("second") == "PASS"
        and bool(res.get("finalized_only_after_pass"))
    )
    return (
        "verification_convergence_fail_then_pass",
        ok,
        f"first={res.get('first')} second={res.get('second')} "
        f"gated={res.get('finalized_only_after_pass')}",
    )


def check_checkpoint(res: dict) -> tuple:
    ok = (
        res.get("checkpoints", 0) >= 3
        and bool(res.get("latest_ok"))
        and bool(res.get("no_loss"))
        and bool(res.get("continued"))
        and bool(res.get("missing_control_ok"))
    )
    return (
        "checkpoint_resume_latest_valid",
        ok,
        f"checkpoints={res.get('checkpoints')} latest_ok={res.get('latest_ok')} "
        f"no_loss={res.get('no_loss')} continued={res.get('continued')}",
    )


def run_all(results: dict, metrics: dict, elapsed_s: float, target_s: float) -> list:
    names = [
        (DURATION_CHECK_NAME, *check_duration(elapsed_s, target_s)[1:]),
        check_context(results["context_pressure"]),
        check_provider(results["provider_failover"]),
        check_replan(results["tool_failure_replan"]),
        check_restart(results["supervisor_restart"]),
        check_side_effect(results["side_effect_ledger"], metrics),
        check_verification(results["verification_convergence"]),
        check_checkpoint(results["checkpoint_resume"]),
    ]
    out = []
    for name, passed, detail in names:
        out.append({"name": name, "passed": bool(passed), "detail": detail})
    return out


def split_gates(checks: list) -> tuple[list, list]:
    """Split HARNESS_MECHANICS checks from the DURATION_GATE check."""
    mechanics = [c for c in checks if c["name"] in MECHANICS_CHECK_NAMES]
    duration = [c for c in checks if c["name"] == DURATION_CHECK_NAME]
    return mechanics, duration
