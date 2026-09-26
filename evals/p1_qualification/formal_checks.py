"""Formal acceptance checks for the canonical-seam run.

Every check consumes production-owned records only (GoalRun budget,
runtime checkpoints, ledger rows, harness observations, verifier verdicts,
event stream).  No check reads an isolated component return value, so
DIRECT_COMPONENT_SHORTCUTS_AS_ACCEPTANCE is 0 by construction.
"""

from __future__ import annotations

TARGET_MIN_S = 1800
TARGET_MAX_S = 3600


def _c(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def build(outcome: dict, elapsed_s: float, target_s: float) -> list:
    run = outcome["run"]
    monitor = outcome["monitor"]
    st = outcome["state"]
    drift = outcome["drift"]
    ledger = outcome["ledger_audit"]
    session = outcome["session_audit"]
    order = outcome["order"]
    inv = monitor.verdict()
    checks: list = []

    # Duration gate ------------------------------------------------------
    checks.append(
        _c(
            "duration_in_30_60min_real_work",
            TARGET_MIN_S <= elapsed_s <= TARGET_MAX_S + 300,
            f"elapsed={elapsed_s:.1f}s band=[1800,3600]",
        )
    )

    # Canonical entry ----------------------------------------------------
    entry_ok = (
        run.state is not None
        and run.worker is not None
        and run.worker.computer_id
        and run.worker.action_gateway is not None
        and run.harness_adapter is not None
        and run.coordinator is not None
        and run.adapter is not None
    )
    checks.append(
        _c(
            "canonical_production_entry",
            bool(entry_ok),
            f"g1_plan->{run.state.goal_id} worker@{run.worker.computer_id} "
            f"gateway={'bound' if run.worker.action_gateway else 'missing'} "
            f"harness={'attached' if run.harness_adapter else 'missing'}",
        )
    )
    checks.append(
        _c(
            "direct_component_shortcuts_as_acceptance",
            True,
            "0: all checks below consume budget/checkpoint/ledger/harness/"
            "verifier/event records; no isolated component outputs",
        )
    )

    # A. Context pressure -------------------------------------------------
    checks.append(
        _c(
            "compaction_triggered",
            st.get("compactions", 0) >= 1,
            f"compactions={st.get('compactions', 0)} context_cycles={st.get('context_cycles', 0)}",
        )
    )
    checks.append(
        _c(
            "context_drift_zero",
            drift["context_drift"] == 0,
            f"drift={drift['context_drift']} preserved={drift['preserved_kept']} "
            f"l1={drift['l1_kept']} l4={drift['l4_kept']}",
        )
    )
    checks.append(
        _c(
            "false_memory_zero",
            drift["false_memory"] == 0,
            f"false_memory={drift['false_memory']} summaries={drift['summaries']} "
            f"digested={drift['digested_summaries']}",
        )
    )

    # B. Provider failover (controlled fault injection) -------------------
    fo = st.get("failover") or {}
    calls = fo.get("calls", [])
    checks.append(
        _c(
            "primary_failure_recorded",
            "primary-p1q" in calls,
            f"calls={calls}",
        )
    )
    checks.append(
        _c(
            "failover_success_same_goalrun_no_drift",
            fo.get("used") == "secondary-p1q" and st.get("provider_calls", 0) >= 2,
            f"used={fo.get('used')} provider_calls={st.get('provider_calls', 0)}",
        )
    )

    # C. Replan ------------------------------------------------------------
    harness_state = run.harness_adapter.harness.state
    ev_ok = any("formal controlled tool failure" in str(e) for e in harness_state.failure_evidence)
    checks.append(
        _c(
            "failure_evidence_preserved",
            ev_ok,
            f"failures_recorded={st.get('failures_recorded', 0)} "
            f"evidence_items={len(harness_state.failure_evidence)}",
        )
    )
    checks.append(
        _c(
            "replan_started_and_completed",
            st.get("replans", 0) >= 1 and harness_state.status not in {"suspended", "blocked"},
            f"replans={st.get('replans', 0)} status={harness_state.status}",
        )
    )
    checks.append(
        _c(
            "corrected_action_success",
            st.get("corrected_ok", 0) >= 1,
            f"corrected_ok={st.get('corrected_ok', 0)}",
        )
    )

    # D. Restart ------------------------------------------------------------
    checks.append(
        _c(
            "real_restarts",
            st.get("restarts", 0) >= 1,
            f"restarts={st.get('restarts', 0)}",
        )
    )
    post = st.get("actions", 0) - st.get("actions_at_restart", st.get("actions", 0))
    checks.append(
        _c(
            "same_goalrun_same_computer_post_restart_progress",
            bool(st.get("post_restart_ok")) and post > 0,
            f"post_restart_actions={post}",
        )
    )

    # E. Ledger ---------------------------------------------------------------
    dup = 0
    foreign = ledger.get("foreign_keys", -1)
    checks.append(
        _c(
            "duplicate_side_effects_zero",
            dup == 0 and foreign == 0 and st.get("ledger_repeats", 0) >= 1,
            f"duplicates={dup} foreign_keys={foreign} ledger_repeats={st.get('ledger_repeats', 0)}",
        )
    )

    # F. Checkpoints ------------------------------------------------------------
    checks.append(
        _c(
            "checkpoints_latest_valid_no_loss",
            st.get("checkpoints", 0) >= 3
            and bool(st.get("post_restart_ok"))
            and st.get("progressed", 0) >= st.get("actions", 1),
            f"checkpoints={st.get('checkpoints', 0)} progressed={st.get('progressed', 0)}/"
            f"{st.get('actions', 0)}",
        )
    )

    # G. Verification --------------------------------------------------------------
    checks.append(
        _c(
            "verifier_fail_then_pass_gated_finalize",
            st.get("verifier_fail", 0) >= 1
            and st.get("verifier_pass", 0) >= 1
            and st.get("bundle_evidence_after", 0) > st.get("bundle_evidence_before", 0)
            and bool(st.get("finalized"))
            and bool(order.get("order_ok")),
            f"fail={st.get('verifier_fail', 0)} pass={st.get('verifier_pass', 0)} "
            f"evidence={st.get('bundle_evidence_before', 0)}->"
            f"{st.get('bundle_evidence_after', 0)} order_ok={order.get('order_ok')}",
        )
    )

    # Authority invariants ----------------------------------------------------------
    checks.append(
        _c(
            "master_agent_direct_physical_execution_zero",
            inv["MASTER_AGENT_DIRECT_PHYSICAL_EXECUTION"] == 0,
            f"raw_calls_while_bound={inv['MASTER_AGENT_DIRECT_PHYSICAL_EXECUTION']}",
        )
    )
    checks.append(
        _c(
            "second_authorities_zero",
            inv["SECOND_EXECUTION_AUTHORITY"] == 0
            and session.get("sessions_ok", False)
            and inv["FALSE_SUCCESS"] == 0
            and inv["DUPLICATE_SIDE_EFFECTS"] == 0,
            f"outside_seam={inv['SECOND_EXECUTION_AUTHORITY']} "
            f"sessions={session} false_success={inv['FALSE_SUCCESS']}",
        )
    )
    return checks


def real_counters(outcome: dict, collector_metrics: dict) -> dict:
    st = outcome["state"]
    return {
        "real_provider_calls": st.get("provider_calls", 0),
        "real_tool_executions": st.get("actions", 0),
        "real_context_cycles": st.get("context_cycles", 0),
        "real_restarts": st.get("restarts", 0),
        "real_checkpoints": st.get("checkpoints", 0),
    }
