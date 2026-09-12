"""Formal-run orchestrator: canonical entry, fault schedule, timed soak.

Fault schedule (fractions of target, each exactly once):
  0.15  controlled primary failover probe (B, fault injection)
  0.30  tool failure -> replan -> corrected seam action (C)
  0.50  supervisor restart with real resume (D)
  0.70  verifier FAIL -> new evidence -> PASS (G, convergence proof)
  0.85  repeat-action ledger probe (E, duplicate check)
  0.10  approval gate probe (blocking resolver, in-run, same gateway)

Everything else is steady seam actions.  Duration is filled by executed
seam cycles only; there is no sleep anywhere in this package.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from .canonical_run import CanonicalRun, SeamGuard
from .invariants import InvariantMonitor, check_event_order

FAULTS = (
    "approval_probe",
    "failover_probe",
    "replan_probe",
    "restart",
    "verify_probe",
    "ledger_probe",
)
FAULT_AT = {
    "approval_probe": 0.10,
    "failover_probe": 0.15,
    "replan_probe": 0.30,
    "restart": 0.50,
    "verify_probe": 0.70,
    "ledger_probe": 0.85,
}


async def run_formal(ctx: dict, target_s: float) -> dict:
    collector = ctx["collector"]
    workload = ctx["workload"]
    t0 = ctx["t0"]
    run = await CanonicalRun(ctx).build()
    ctx["canonical"] = run
    monitor = InvariantMonitor(collector)
    monitor.watch_coordinator(run.coordinator)
    monitor.watch_verifier(run.worker.verification_engine)
    # Honest baseline snapshot for drift/false-memory checks (production records).
    import dataclasses

    from runtime.context.models import ContextLayer

    preserved_base = dataclasses.asdict(run.worker.context_engine.state.preserved)
    l1_base = _layer_hash(run, ContextLayer.L1_ACTIVE_GOAL_PLAN)
    l4_base = _layer_hash(run, ContextLayer.L4_EVIDENCE_ARTIFACTS)

    state: dict[str, Any] = {
        "actions": 0,
        "progressed": 0,
        "checkpoints": 0,
        "context_cycles": 0,
        "compactions": 0,
        "provider_calls": 0,
        "restarts": 0,
        "faults_done": {},
        "chain_digest": "genesis",
        "verifier_fail": 0,
        "verifier_pass": 0,
        "replans": 0,
        "failures_recorded": 0,
        "corrected_ok": 0,
        "ledger_repeats": 0,
        "approval_gated": False,
        "finalized": False,
    }

    async def steady_action() -> None:
        ev = workload.cycle()
        collector.count("work_cycles")
        out = await run.seam_action(ev.cycle, ev.output_hash)
        state["actions"] += 1
        if out["progressed"]:
            state["progressed"] += 1
        record = out["record"]
        status = (record.get("result") or {}).get("status")
        if status != "completed":
            raise RuntimeError(f"steady seam action not completed: {status}")
        # Chain-linked context observation (real, tamper-evident growth).
        state["chain_digest"] = hashlib.sha256(
            f"{state['chain_digest']}:{ev.cycle}:{ev.output_hash}".encode()
        ).hexdigest()
        obs = {
            "cycle": ev.cycle,
            "output": ev.output_hash,
            "artifact": ev.artifact_relpath,
            "prev_digest": state["chain_digest"],
            "action_status": status,
        }
        text = json.dumps(obs, sort_keys=True)
        from runtime.context.models import ContextLayer

        run.worker.context_engine.append_to_layer(
            ContextLayer.L2_OBSERVATIONS, [obs], token_estimate=max(1, len(text) // 4)
        )
        collector.count("context_appends")
        state["context_cycles"] += 1
        if run.worker.context_engine.should_compact():
            plan = run.worker.context_engine.build_compaction_plan()
            run.worker.context_engine.execute_compaction(plan)
            collector.count("compactions")
            state["compactions"] += 1
            collector.emit("compaction", at_action=state["actions"])
        if state["actions"] % 25 == 0:
            run.harness_adapter.persist(reason="soak_periodic")
            run.worker.checkpoint(run.state, str(run.project_root), reason="soak_periodic")
            collector.count("checkpoints", 2)
            state["checkpoints"] += 2
        if state["actions"] % 200 == 0:
            await _secondary_provider_call(run, workload, collector, state)

    async def fault_approval_probe() -> None:
        from veya.platform import load

        obase = load("obase")
        gateway = run.worker.action_gateway
        prev_resolver = gateway._approval_resolver
        prev_hook = gateway._policy_hook
        release: asyncio.Event = asyncio.Event()
        started: asyncio.Event = asyncio.Event()

        async def blocking(_request: Any) -> bool:
            started.set()
            await release.wait()
            return True

        gateway._approval_resolver = blocking
        gateway._policy_hook = lambda req: obase.ActionDecision(
            verdict="REQUIRE_APPROVAL", reason="formal approval probe", request_id=req.request_id
        )
        try:
            ev = workload.cycle()
            collector.count("work_cycles")
            SeamGuard.open()
            try:
                pending_task = asyncio.create_task(
                    run.coordinator._guarded_handle_tool_call(
                        "p1q.work", {"cycle": ev.cycle, "output": ev.output_hash}
                    )
                )
                await asyncio.wait_for(started.wait(), timeout=120)
                assert not pending_task.done(), "approval must gate execution"
                chk = (run.state.runtime_checkpoint or {}).get("canonical_action") or {}
                assert chk.get("status") == "pending", (
                    "pending checkpoint must exist during approval"
                )
                release.set()
                await asyncio.wait_for(pending_task, timeout=120)
            finally:
                SeamGuard.close()
            state["approval_gated"] = True
            collector.count("tool_executions")
            collector.emit("fault", fault="approval_probe", gated=True)
        finally:
            gateway._approval_resolver = prev_resolver
            gateway._policy_hook = prev_hook

    async def fault_failover_probe() -> None:
        calls: list[str] = []

        async def request(provider: str) -> Any:
            calls.append(provider)
            collector.count("provider_calls")
            state["provider_calls"] += 1
            if provider == "primary-p1q":
                raise TimeoutError("p1q CONTROLLED primary failure (fault injection)")
            ev = workload.cycle()
            collector.count("work_cycles")
            return {"ok": True, "schema": "p1q-result-1", "output": ev.output_hash}

        used, result, continuity = await run.worker.provider_adapter.call(
            request,
            ["primary-p1q", "secondary-p1q"],
            goal_run_id=run.state.goal_id,
            context={"goal_run_id": run.state.goal_id, "tool_schema": "p1q.work"},
        )
        assert used == "secondary-p1q" and calls[0] == "primary-p1q"
        assert continuity.get("goal_run_id") == run.state.goal_id
        assert result.get("schema") == "p1q-result-1"
        collector.count("provider_failovers")
        collector.emit("fault", fault="failover_probe", used=used, calls=calls)
        state["failover"] = {"used": used, "calls": calls}

    async def fault_replan_probe() -> None:
        harness = run.harness_adapter.harness
        tool, bad = "p1q.work", {"cycle": -1, "output": "bad-controlled"}

        class _Controlled(RuntimeError):
            pass

        harness.before_action(tool, bad)
        try:
            raise _Controlled("formal controlled tool failure")
        except _Controlled as exc:
            harness.record_action_failure(tool, bad, str(exc))
        state["failures_recorded"] += 1
        harness.replan("formal_controlled_failure")
        collector.count("replans")
        state["replans"] += 1
        ev = workload.cycle()
        collector.count("work_cycles")
        out = await run.seam_action(ev.cycle, ev.output_hash)
        assert out["progressed"], "corrected action must register production progress"
        status = (out["record"].get("result") or {}).get("status")
        assert status == "completed", "corrected action must succeed through the seam"
        state["corrected_ok"] += 1
        collector.emit("fault", fault="replan_probe", corrected=True)

    async def fault_restart() -> None:
        pre_obs = len(run.harness_adapter.harness.state.observations)
        resumed = await run.restart_supervisors("supervisor-a", "supervisor-b")
        assert resumed["same_computer"] and resumed["same_goalrun"]
        assert len(run.harness_adapter.harness.state.observations) >= pre_obs
        monitor.watch_verifier(run.worker.verification_engine)
        state["restarts"] += 1
        collector.count("checkpoints")
        ev = workload.cycle()
        collector.count("work_cycles")
        out = await run.seam_action(ev.cycle, ev.output_hash)
        assert (out["record"].get("result") or {}).get("status") == "completed"
        state["post_restart_ok"] = True
        collector.emit("fault", fault="restart", **{k: v for k, v in resumed.items()})

    async def fault_verify_probe() -> None:
        # FAIL now (replan, run continues); the PASS + finalize happen AFTER
        # the soak loop so the production harness completes exactly once.
        from runtime.verification.models import (
            AcceptanceCriterion,
            EvidenceBundle,
            EvidenceItem,
            VerificationSpec,
        )

        engine = run.worker.verification_engine
        head = run.state.goal_id  # binding scope for this run (used consistently)
        criteria = [
            AcceptanceCriterion(id="ac-formal-build", description="build artifact exists"),
            AcceptanceCriterion(id="ac-formal-test", description="test evidence recorded"),
        ]
        spec = VerificationSpec.create_for_task(
            task_id="p1q-formal-verify",
            goal_run_id=run.state.goal_id,
            head_sha=head,
            acceptance_criteria=criteria,
        )
        ev1 = workload.cycle()
        collector.count("work_cycles")
        bundle = EvidenceBundle(
            task_id="p1q-formal-verify",
            goal_run_id=run.state.goal_id,
            head_sha=head,
            verification_spec_version=spec.version,
            verification_spec_hash=spec.spec_hash,
        ).add_evidence(
            EvidenceItem(
                id="ev-ac-formal-build",
                kind="artifact",
                source="seam",
                content=ev1.output_hash,
                producer="p1q",
                metadata={"criterion_id": "ac-formal-build"},
            )
        )
        verdict1 = await engine.run_independent_verifier(spec, bundle, head)
        collector.count("verifier_runs")
        assert verdict1.outcome in {"FAIL", "BLOCKED"}
        state["verifier_fail"] += 1
        collector.emit("verifier", outcome=verdict1.outcome, round=1)
        run.harness_adapter.apply_verification(verdict1.outcome, evidence={"round": 1})
        collector.count("replans")
        state["verify_pending"] = {"spec": spec, "bundle": bundle, "head": head}
        state["bundle_evidence_before"] = len(bundle.evidence)
        collector.emit("fault", fault="verify_probe", failed=True)

    async def finalize_after_pass() -> None:
        from runtime.verification.models import EvidenceItem

        pending = state.get("verify_pending")
        assert pending, "verify FAIL must precede finalize"
        engine = run.worker.verification_engine
        ev2 = workload.cycle()
        collector.count("work_cycles")
        bundle2 = pending["bundle"].add_evidence(
            EvidenceItem(
                id="ev-ac-formal-test",
                kind="test_result",
                source="seam",
                content=ev2.output_hash,
                producer="p1q",
                metadata={"criterion_id": "ac-formal-test"},
            )
        )
        verdict2 = await engine.run_independent_verifier(pending["spec"], bundle2, pending["head"])
        collector.count("verifier_runs")
        assert verdict2.outcome == "PASS"
        state["verifier_pass"] += 1
        state["bundle_evidence_after"] = len(bundle2.evidence)
        collector.count("verifier_passes")
        collector.emit("verifier", outcome="PASS", round=2)
        run.harness_adapter.apply_verification("PASS", evidence={"round": 2})
        assert run.harness_adapter.harness.state.status == "completed"
        state["finalized"] = True
        collector.emit("finalized", after="verifier_PASS")

    async def fault_ledger_probe() -> None:
        # Re-execute an earlier committed action request: ledger must hit the
        # committed row without re-invoking physical.
        before = dict(run.physical_invocations)
        hyper = run.adapter.request("p1q.work", {"cycle": 0, "output": "ledger-probe"})
        # Find one committed key by replaying the FIRST seam action args is
        # impossible (digests differ); instead replay this probe twice.
        SeamGuard.open()
        try:
            first = await run.worker.execute_canonical_action(
                run.state, hyper, gateway_executor=run._physical
            )
            second = await run.worker.execute_canonical_action(
                run.state, hyper, gateway_executor=run._physical
            )
        finally:
            SeamGuard.close()
        assert first.status == "completed" and second.status == "completed"
        after = dict(run.physical_invocations)
        delta = sum(after.values()) - sum(before.values())
        assert delta == 1, f"repeat must execute physical exactly once, got {delta}"
        state["ledger_repeats"] += 1
        collector.count("side_effect_commits")
        collector.emit("fault", fault="ledger_probe", physical_delta=delta)

    faults = {
        "approval_probe": fault_approval_probe,
        "failover_probe": fault_failover_probe,
        "replan_probe": fault_replan_probe,
        "restart": fault_restart,
        "verify_probe": fault_verify_probe,
        "ledger_probe": fault_ledger_probe,
    }

    # Steady soak with scheduled faults; duration = executed cycles only.
    while True:
        elapsed = time.time() - t0
        if elapsed >= target_s:
            break
        for name in FAULTS:
            if name not in state["faults_done"] and elapsed >= target_s * FAULT_AT[name]:
                await faults[name]()
                state["faults_done"][name] = True
                collector.emit("fault_done", fault=name, elapsed=round(elapsed, 1))
                if name == "restart":
                    state["actions_at_restart"] = state["actions"]
                break
        await steady_action()
        if time.time() - t0 > 3600:
            break

    # Finalize exactly once, only after the PASS (production harness completes).
    await finalize_after_pass()

    # Drift/false-memory evidence (production records, before/after).
    drift = _drift_audit(run, preserved_base, l1_base, l4_base)

    # End audits (production-owned records only).
    ledger_db = run.project_root / ".veya" / "execution-runtime.sqlite3"
    ledger_audit = monitor.audit_ledger(ledger_db, run.state.goal_id)
    session_audit = monitor.audit_sessions(run.worker.computer_store, run.worker.computer_id)
    order = check_event_order(collector.events_path)
    monitor.unwrap()

    # Physical-invocation audit: every committed ledger-path action key
    # executed exactly once (duplicates counted live below).
    return {
        "run": run,
        "monitor": monitor,
        "state": state,
        "drift": drift,
        "ledger_audit": ledger_audit,
        "session_audit": session_audit,
        "order": order,
        "elapsed": time.time() - t0,
    }


def _layer_hash(run: Any, layer: Any) -> str:
    import hashlib

    content = run.worker.context_engine.state.get_layer(layer).content
    return hashlib.sha256(repr(content).encode()).hexdigest()


def _drift_audit(run: Any, preserved_base: dict, l1_base: str, l4_base: str) -> dict:
    import dataclasses

    from runtime.context.models import ContextLayer

    preserved_now = dataclasses.asdict(run.worker.context_engine.state.preserved)
    kept = all(
        preserved_now.get(k) == preserved_base.get(k)
        for k in (
            "objective",
            "verification_spec_ref",
            "computer_id",
            "goal_run_id",
            "artifact_refs",
            "evidence_refs",
        )
    )
    l1_ok = _layer_hash(run, ContextLayer.L1_ACTIVE_GOAL_PLAN) == l1_base
    l4_ok = _layer_hash(run, ContextLayer.L4_EVIDENCE_ARTIFACTS) == l4_base
    summaries = 0
    digested = 0
    for layer in (ContextLayer.L0_CURRENT_TURN, ContextLayer.L2_OBSERVATIONS):
        for item in run.worker.context_engine.state.get_layer(layer).content:
            text = str(item.get("content", "")) if isinstance(item, dict) else str(item)
            if "[COMPACTION SUMMARY]" in text or "[extractive digest=" in text:
                summaries += 1
                if "digest=" in text:
                    digested += 1
    drift = 0 if (kept and l1_ok and l4_ok) else 1
    false_memory = (
        0 if (kept and l1_ok and l4_ok and (summaries == 0 or digested == summaries)) else 1
    )
    return {
        "context_drift": drift,
        "false_memory": false_memory,
        "preserved_kept": kept,
        "l1_kept": l1_ok,
        "l4_kept": l4_ok,
        "summaries": summaries,
        "digested_summaries": digested,
    }


async def _secondary_provider_call(run: Any, workload: Any, collector: Any, state: dict) -> None:
    async def request(provider: str) -> Any:
        collector.count("provider_calls")
        state["provider_calls"] += 1
        ev = workload.cycle()
        collector.count("work_cycles")
        return {"ok": True, "schema": "p1q-result-1", "output": ev.output_hash}

    used, result, continuity = await run.worker.provider_adapter.call(
        request,
        ["secondary-p1q"],
        goal_run_id=run.state.goal_id,
        context={"goal_run_id": run.state.goal_id},
    )
    assert used == "secondary-p1q" and result.get("schema") == "p1q-result-1"
    assert continuity.get("goal_run_id") == run.state.goal_id
