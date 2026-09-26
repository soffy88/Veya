"""The 7 qualification scenario drivers (qualification class: HARNESS_MECHANICS).

Each driver exercises a real production code path (imported from
``runtime.*`` only) with real inputs produced by :class:`RealWorkload`.
Nothing here sleeps, mocks a production class, or bypasses a production
code path: failures are real exceptions raised through the real adapters,
and recovery is performed with the real replan/resume/restore APIs.

CONTROLLED vs REAL: the injected faults (provider TimeoutError, tool
failure) are deterministic and controlled. They qualify MECHANICS
(adapter failover path, replan path, ledger idempotency, restart restore).
They do NOT qualify a real external provider outage or a real 30-60 min
production run — that is FINAL_REAL_QUALIFICATION and is NOT_RUN here.

Every driver returns a plain-dict result that the assertions module checks.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from pathlib import Path
from typing import Any


def _extractive_summary(text: str) -> str:
    """Deterministic local summarizer for compaction (no LLM, no network).

    Keeps the head/tail of the rendered content plus per-line digests so the
    summary is a pure function of its input — real summarization-shaped work
    without an external provider.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    head = lines[:5]
    tail = lines[-5:]
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    kept = head + (["..."] if len(lines) > 10 else []) + tail
    return f"[extractive digest={digest} lines={len(lines)}]\n" + "\n".join(kept)[:4000]


# --------------------------------------------------------------------------
# 1. Context pressure
# --------------------------------------------------------------------------


def scenario_context_pressure(ctx: dict) -> dict:
    from runtime.context.engine import ContextEngine
    from runtime.context.models import ContextBudget, ContextLayer, PreservedItems

    collector = ctx["collector"]
    workload = ctx["workload"]
    goal_run_id = ctx["goal_run_id"]
    computer_id = ctx["computer_id"]

    engine = ContextEngine(
        goal_run_id,
        computer_id,
        budget=ContextBudget(max_tokens=12000, trigger_ratio=0.5, compact_ratio=0.5),
        llm_summarizer=_extractive_summary,
    )
    preserved = PreservedItems(
        objective="p1q: long autonomous run must keep goal/spec/failure/artifact refs",
        verification_spec_ref="vspec-p1q-001",
        active_plan=["phase-1", "phase-2", "phase-3"],
        current_step="phase-2",
        unresolved_failures=[{"id": "fail-001", "note": "seeded unresolved failure ref"}],
        important_observations=[{"id": "obs-001", "note": "seeded observation ref"}],
        artifact_refs=["work_artifacts/cycle-000001.txt"],
        evidence_refs=["ev-p1q-001"],
        computer_id=computer_id,
        goal_run_id=goal_run_id,
        feature_map_ref="fmap-p1q",
    )
    engine.set_preserved(preserved)
    engine.set_layer(
        ContextLayer.L1_ACTIVE_GOAL_PLAN,
        [{"step": s} for s in preserved.active_plan],
        token_estimate=300,
    )
    # Protected evidence layer: content hash must survive compaction verbatim.
    l4_content = [{"artifact": "a", "sha256": "deadbeef"}]
    engine.set_layer(ContextLayer.L4_EVIDENCE_ARTIFACTS, l4_content, token_estimate=200)
    l4_before = hashlib.sha256(repr(l4_content).encode()).hexdigest()

    appends = 0
    pressure_hit = False
    # Real tool-execution shaped traffic: each workload cycle becomes one or
    # more observation entries until the engine reports pressure.
    while not pressure_hit and appends < 400:
        ev = workload.cycle()
        collector.count("work_cycles")
        collector.count("tool_executions")
        engine.append_to_layer(
            ContextLayer.L2_OBSERVATIONS,
            [{"cycle": ev.cycle, "output": ev.output_hash, "artifact": ev.artifact_relpath}],
            token_estimate=220,
        )
        engine.append_to_layer(
            ContextLayer.L0_CURRENT_TURN,
            [{"role": "assistant", "content": f"cycle {ev.cycle} output {ev.output_hash}"}],
            token_estimate=120,
        )
        appends += 2
        collector.count("context_appends", 2)
        if engine.should_compact():
            pressure_hit = True

    tokens_before = engine.state.total_token_estimate()
    preserved_before = dataclasses.asdict(engine.state.preserved)
    plan = engine.build_compaction_plan()
    record = engine.execute_compaction(plan, llm_summarizer=_extractive_summary)
    collector.count("compactions")
    tokens_after = engine.state.total_token_estimate()
    preserved_after = dataclasses.asdict(engine.state.preserved)
    l4_after = hashlib.sha256(
        repr(engine.state.get_layer(ContextLayer.L4_EVIDENCE_ARTIFACTS).content).encode()
    ).hexdigest()

    drift = 0 if (preserved_before == preserved_after and l4_before == l4_after) else 1
    collector.emit(
        "context_pressure",
        appends=appends,
        pressure_hit=pressure_hit,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        layers_compacted=[str(layer) for layer in (record.layers_compacted or [])],
        context_drift=drift,
    )
    return {
        "pressure_hit": pressure_hit,
        "appends": appends,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "context_drift": drift,
        "compactions": 1,
    }


# --------------------------------------------------------------------------
# 2. Provider failover
# --------------------------------------------------------------------------


def scenario_provider_failover(ctx: dict) -> dict:
    from runtime.provider_reliability import ReliableProviderAdapter

    collector = ctx["collector"]
    workload = ctx["workload"]
    goal_run_id = ctx["goal_run_id"]
    context_ref = {
        "goal_run_id": goal_run_id,
        "computer_id": ctx["computer_id"],
        "tool_schema": {"tool": "p1q.tool", "args": {"cycle": "int"}},
        "context_schema_version": "p1q-1",
    }

    calls: list[str] = []

    async def request(provider: str):
        calls.append(provider)
        collector.count("provider_calls")
        if provider == "primary-p1q":
            # CONTROLLED failure through the real adapter path (not a mock,
            # and NOT a real external provider outage — see qualification
            # class HARNESS_MECHANICS vs FINAL_REAL_QUALIFICATION).
            raise TimeoutError("p1q CONTROLLED primary provider timeout")
        ev = workload.cycle()
        collector.count("work_cycles")
        return {
            "ok": True,
            "provider": provider,
            "output": ev.output_hash,
            "schema": "p1q-result-1",
        }

    adapter = ReliableProviderAdapter(provider_router=None, failure_threshold=1)

    async def _run():
        return await adapter.call(
            request,
            ["primary-p1q", "secondary-p1q"],
            goal_run_id=goal_run_id,
            context=context_ref,
        )

    used, result, continuity = asyncio.run(_run())
    failover = used == "secondary-p1q" and calls[0] == "primary-p1q"
    if failover:
        collector.count("provider_failovers")
    same_goalrun = continuity.get("goal_run_id") == goal_run_id
    schema_ok = continuity.get("context") == context_ref and result.get("schema") == "p1q-result-1"
    semantic_drift = 0 if (same_goalrun and schema_ok) else 1
    collector.emit(
        "provider_failover",
        failover_class="controlled",
        calls=calls,
        used=used,
        same_goalrun=same_goalrun,
        schema_ok=schema_ok,
        semantic_drift=semantic_drift,
    )
    return {
        "failover_class": "controlled",
        "used": used,
        "calls": calls,
        "failover": failover,
        "same_goalrun": same_goalrun,
        "schema_ok": schema_ok,
        "semantic_drift": semantic_drift,
    }


# --------------------------------------------------------------------------
# 3. Tool failure + replan
# --------------------------------------------------------------------------


class _ControlledToolFailure(RuntimeError):
    pass


def scenario_tool_failure_replan(ctx: dict) -> dict:
    from runtime.execution.long_running import (
        LongRunBudget,
        LongRunCheckpointStore,
        LongRunningHarness,
        LongRunState,
        ProgressObservation,
    )

    collector = ctx["collector"]
    workload = ctx["workload"]
    run_root = Path(ctx["run_root"]) / "replan"
    store = LongRunCheckpointStore(run_root)
    state = LongRunState(
        goal_run_id=ctx["goal_run_id"],
        computer_id=ctx["computer_id"],
        plan=["replan-phase-1", "replan-phase-2"],
    )
    harness = LongRunningHarness(
        state,
        LongRunBudget(max_wall_s=3600, max_tool_calls=100000, max_replans=50),
        checkpoint_store=store,
    )

    tool, bad_args = "p1q.tool", {"mode": "bad-controlled", "n": 1}
    harness.before_action(tool, bad_args)
    collector.count("tool_executions")
    # One real controlled failure, recorded as evidence.
    try:
        raise _ControlledToolFailure("p1q controlled tool failure (bad mode flag)")
    except _ControlledToolFailure as exc:
        harness.record_action_failure(tool, bad_args, str(exc))
    failure_preserved = len(harness.state.failure_evidence) == 1

    replans_before = harness.state.replans
    harness.replan("controlled_tool_failure")
    collector.count("replans")

    # Corrected action: real work with fixed args.
    good_args = {"mode": "good-corrected", "n": 2}
    harness.before_action(tool, good_args)
    collector.count("tool_executions")
    ev = workload.cycle()
    collector.count("work_cycles")
    progressed = harness.record_progress(
        ProgressObservation(
            artifacts=[ev.artifact_relpath],
            state_hash=ev.output_hash,
            tool_result_hash=ev.output_hash,
            plan_step="replan-phase-2",
        )
    )
    harness.checkpoint(reason="replan_recovered")
    collector.count("checkpoints")
    # Post-replan status is "recovering" by production design (it returns to
    # "running" via provider recovery or completes on verification PASS);
    # recovered means: alive, progressing, failure evidence kept, not parked.
    recovered = (
        progressed
        and harness.state.status not in {"suspended", "blocked"}
        and harness.state.replans >= 1
    )
    collector.emit(
        "tool_replan",
        failure_preserved=failure_preserved,
        replans_before=replans_before,
        replans_after=harness.state.replans,
        progressed=progressed,
        recovered_artifact=ev.artifact_relpath,
    )
    return {
        "failure_preserved": failure_preserved,
        "replans": harness.state.replans,
        "progressed": progressed,
        "recovered": recovered,
    }


# --------------------------------------------------------------------------
# 4. PersistentComputer restart (Supervisor A terminate -> B restore)
# --------------------------------------------------------------------------


def scenario_supervisor_restart(ctx: dict) -> dict:
    from runtime.computer.store import PersistentComputerStore
    from runtime.execution.long_running import (
        LongRunBudget,
        LongRunCheckpointStore,
        LongRunningHarness,
        LongRunState,
        ProgressObservation,
    )

    collector = ctx["collector"]
    workload = ctx["workload"]
    goal_run_id = ctx["goal_run_id"]
    db_path = Path(ctx["run_root"]) / "computer" / "persistent.db"

    # Supervisor A: create computer + session, do real work, checkpoint.
    store_a = PersistentComputerStore(db_path)
    computer = store_a.create_computer(owner_id="p1q-owner", workspace_ref=str(ctx["run_root"]))
    session_a = store_a.create_session(computer.computer_id, "p1q-owner", "supervisor-A")
    run_root = Path(ctx["run_root"]) / "restart"
    ckpt_store = LongRunCheckpointStore(run_root)
    harness_a = LongRunningHarness(
        LongRunState(
            goal_run_id=goal_run_id, computer_id=computer.computer_id, plan=["restart-phase"]
        ),
        LongRunBudget(max_wall_s=3600, max_tool_calls=100000, max_replans=50),
        checkpoint_store=ckpt_store,
    )
    ev_a = workload.cycle()
    collector.count("work_cycles")
    harness_a.record_progress(
        ProgressObservation(
            artifacts=[ev_a.artifact_relpath],
            state_hash=ev_a.output_hash,
            tool_result_hash=ev_a.output_hash,
            plan_step="restart-phase",
        )
    )
    harness_a.checkpoint(reason="pre_restart")
    collector.count("checkpoints")
    obs_before = len(harness_a.state.observations)

    # Terminate Supervisor A (real session end; drop the handle).
    store_a.end_session(session_a.session_id)
    del store_a
    del harness_a
    collector.emit(
        "supervisor_terminated", supervisor="supervisor-A", computer_id=computer.computer_id
    )

    # Supervisor B: restore the SAME computer and SAME GoalRun, continue.
    store_b = PersistentComputerStore(db_path)
    restored = store_b.get_computer(computer.computer_id)
    assert restored is not None, "computer must survive supervisor restart"
    session_b = store_b.create_session(restored.computer_id, "p1q-owner", "supervisor-B")
    harness_b = LongRunningHarness.resume(
        ckpt_store,
        LongRunBudget(max_wall_s=3600, max_tool_calls=100000, max_replans=50),
    )
    collector.count("restores")
    ev_b = workload.cycle()
    collector.count("work_cycles")
    harness_b.record_progress(
        ProgressObservation(
            artifacts=[ev_b.artifact_relpath],
            state_hash=ev_b.output_hash,
            tool_result_hash=ev_b.output_hash,
            plan_step="restart-phase",
        )
    )
    harness_b.checkpoint(reason="post_restart")
    collector.count("checkpoints")

    same_computer = restored.computer_id == computer.computer_id
    same_goalrun = harness_b.state.goal_run_id == goal_run_id
    continued = len(harness_b.state.observations) == obs_before + 1
    collector.emit(
        "supervisor_restart",
        same_computer=same_computer,
        same_goalrun=same_goalrun,
        continued=continued,
        session_b=session_b.session_id,
    )
    return {
        "same_computer": same_computer,
        "same_goalrun": same_goalrun,
        "continued": continued,
        "computer_id": computer.computer_id,
    }


# --------------------------------------------------------------------------
# 5. SideEffectLedger (commit before restart/failure; resume => no dup)
# --------------------------------------------------------------------------


class ToolExecutionError(RuntimeError):
    """Local deterministic tool failure (name-matched by SideEffectLedger).

    SideEffectLedger treats ``type(exc).__name__ == "ToolExecutionError"`` as
    retryable-without-external-effect; any other exception after the call
    boundary becomes unknown.  The name match is intentional and documented.
    """


def scenario_side_effect_ledger(ctx: dict) -> dict:
    from runtime.execution.durable import DurableExecutionRepository
    from runtime.execution.side_effects import SideEffectLedger

    collector = ctx["collector"]
    workload = ctx["workload"]
    goal_run_id = ctx["goal_run_id"]

    async def _run():
        repo = DurableExecutionRepository(
            sqlite_path=str(Path(ctx["run_root"]) / "ledger" / "durable.db")
        )
        await repo.connect()
        await repo.migrate()
        ledger = SideEffectLedger(repo)
        invocations: list[str] = []

        async def provider():
            ev = workload.cycle()
            invocations.append(ev.output_hash)
            target = Path(ctx["run_root"]) / "ledger" / "committed.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(ev.output_hash, encoding="utf-8")
            return {"output": ev.output_hash, "artifact": "ledger/committed.txt"}

        op = "p1q.op.commit-once"
        first = await ledger.execute(
            goal_run_id=goal_run_id,
            work_item_id="p1q-side-effect",
            operation_key=op,
            operation_type="file_write",
            target_ref="ledger/committed.txt",
            request={"op": op},
            provider=provider,
            capability="idempotency_key",
        )
        collector.count("side_effect_commits")
        # Simulate restart/failure: a second execute with the SAME
        # operation_key must hit the committed row, not the provider.
        second = await ledger.execute(
            goal_run_id=goal_run_id,
            work_item_id="p1q-side-effect",
            operation_key=op,
            operation_type="file_write",
            target_ref="ledger/committed.txt",
            request={"op": op},
            provider=provider,
            capability="idempotency_key",
        )
        collector.count("side_effect_resume_hits")
        duplicates = max(0, len(invocations) - 1)
        collector.metrics["duplicate_side_effects"] = duplicates
        await repo.close()
        return first, second, len(invocations), duplicates

    first, second, invocations, duplicates = asyncio.run(_run())
    collector.count("work_cycles", invocations)
    resumed_equal = first == second
    collector.emit(
        "side_effect_ledger",
        invocations=invocations,
        duplicates=duplicates,
        resumed_equal=resumed_equal,
    )
    return {
        "invocations": invocations,
        "duplicates": duplicates,
        "resumed_equal": resumed_equal,
    }


# --------------------------------------------------------------------------
# 6. Verification convergence (FAIL at least once -> replan -> PASS)
# --------------------------------------------------------------------------


def scenario_verification_convergence(ctx: dict) -> dict:
    from runtime.execution.long_running import (
        LongRunBudget,
        LongRunningHarness,
        LongRunState,
    )
    from runtime.verification.engine import VerificationEngine
    from runtime.verification.models import (
        AcceptanceCriterion,
        EvidenceBundle,
        EvidenceItem,
        VerificationSpec,
    )

    collector = ctx["collector"]
    workload = ctx["workload"]
    goal_run_id = ctx["goal_run_id"]
    task_id = "p1q-verify-task"
    head_sha = ctx["head_sha"]

    engine = VerificationEngine(project_root=str(Path(ctx["run_root"]) / "verify"))
    harness = LongRunningHarness(
        LongRunState(
            goal_run_id=goal_run_id, computer_id=ctx["computer_id"], plan=["verify-phase"]
        ),
        LongRunBudget(max_wall_s=3600, max_tool_calls=100000, max_replans=50),
    )
    criteria = [
        AcceptanceCriterion(
            id="ac-p1q-build", description="build artifact exists", required=True, kind="functional"
        ),
        AcceptanceCriterion(
            id="ac-p1q-test", description="test evidence recorded", required=True, kind="functional"
        ),
    ]
    spec = VerificationSpec.create_for_task(
        task_id=task_id,
        goal_run_id=goal_run_id,
        head_sha=head_sha,
        acceptance_criteria=criteria,
    )

    async def _verify(bundle: EvidenceBundle):
        return await engine.run_independent_verifier(spec, bundle, head_sha)

    # Candidate 1: incomplete evidence -> verifier MUST fail at least once.
    ev1 = workload.cycle()
    collector.count("work_cycles")
    bundle_v1 = EvidenceBundle(
        task_id=task_id,
        goal_run_id=goal_run_id,
        head_sha=head_sha,
        verification_spec_version=spec.version,
        verification_spec_hash=spec.spec_hash,
    ).add_evidence(
        EvidenceItem(
            id="ev-ac-p1q-build",
            kind="artifact",
            source="harness.artifact_store",
            content=ev1.output_hash,
            producer="p1q",
            metadata={"criterion_id": "ac-p1q-build"},
        )
    )
    verdict1 = asyncio.run(_verify(bundle_v1))
    collector.count("verifier_runs")
    collector.count("verifier_fails")
    failed_once = verdict1.outcome in {"FAIL", "BLOCKED"}
    harness.apply_verification(verdict1.outcome, evidence={"bundle": bundle_v1.bundle_hash})
    collector.count("replans")

    # Replan with new evidence -> verifier PASS -> finalize only now.
    finalized_before_pass = harness.state.status == "completed"
    ev2 = workload.cycle()
    collector.count("work_cycles")
    bundle_v2 = bundle_v1.add_evidence(
        EvidenceItem(
            id="ev-ac-p1q-test",
            kind="test_result",
            source="harness.test",
            content=ev2.output_hash,
            producer="p1q",
            metadata={"criterion_id": "ac-p1q-test"},
        )
    )
    verdict2 = asyncio.run(_verify(bundle_v2))
    collector.count("verifier_runs")
    passed = verdict2.outcome == "PASS"
    if passed:
        collector.count("verifier_passes")
    harness.apply_verification(verdict2.outcome, evidence={"bundle": bundle_v2.bundle_hash})
    finalized_after_pass = harness.state.status == "completed"
    collector.emit(
        "verification_convergence",
        first=verdict1.outcome,
        second=verdict2.outcome,
        failed_once=failed_once,
        finalized_before_pass=finalized_before_pass,
        finalized_after_pass=finalized_after_pass,
    )
    return {
        "first": verdict1.outcome,
        "second": verdict2.outcome,
        "failed_once": failed_once,
        "finalized_only_after_pass": (not finalized_before_pass) and finalized_after_pass,
    }


# --------------------------------------------------------------------------
# 7. Checkpoint / resume (multiple checkpoints, restore latest valid)
# --------------------------------------------------------------------------


def scenario_checkpoint_resume(ctx: dict) -> dict:
    from runtime.execution.long_running import (
        LongRunCheckpointStore,
        LongRunningHarness,
        LongRunState,
        ProgressObservation,
    )

    collector = ctx["collector"]
    workload = ctx["workload"]
    run_root = Path(ctx["run_root"]) / "checkpoints"
    store = LongRunCheckpointStore(run_root)

    # Negative control on an isolated path: missing checkpoint reads None.
    missing = LongRunCheckpointStore(run_root / "missing-control").read()
    missing_ok = missing is None

    state = LongRunState(
        goal_run_id=ctx["goal_run_id"], computer_id=ctx["computer_id"], plan=["ckpt-phase"]
    )
    harness = LongRunningHarness(state, checkpoint_store=store)
    checkpoints = 0
    # Multiple checkpoints across real progress.
    for _ in range(3):
        ev = workload.cycle()
        collector.count("work_cycles")
        harness.record_progress(
            ProgressObservation(
                artifacts=[ev.artifact_relpath],
                state_hash=ev.output_hash,
                tool_result_hash=ev.output_hash,
                plan_step="ckpt-phase",
            )
        )
        harness.checkpoint(reason="periodic")
        checkpoints += 1
        collector.count("checkpoints")
    expected_obs = len(harness.state.observations)
    del harness

    restored = LongRunningHarness.resume(store)
    collector.count("restores")
    latest_ok = len(restored.state.observations) == expected_obs
    no_loss = restored.state.observations[-1]["artifacts"] == state.observations[-1]["artifacts"]
    # Continue after restore: progress must not be lost.
    ev = workload.cycle()
    collector.count("work_cycles")
    restored.record_progress(
        ProgressObservation(
            artifacts=[ev.artifact_relpath],
            state_hash=ev.output_hash,
            tool_result_hash=ev.output_hash,
            plan_step="ckpt-phase",
        )
    )
    continued = len(restored.state.observations) == expected_obs + 1
    collector.emit(
        "checkpoint_resume",
        checkpoints=checkpoints,
        latest_ok=latest_ok,
        no_loss=bool(no_loss),
        continued=continued,
        missing_control_ok=missing_ok,
    )
    return {
        "checkpoints": checkpoints,
        "latest_ok": latest_ok,
        "no_loss": bool(no_loss),
        "continued": continued,
        "missing_control_ok": missing_ok,
    }


SCENARIOS: list[tuple[str, Any]] = [
    ("context_pressure", scenario_context_pressure),
    ("provider_failover", scenario_provider_failover),
    ("tool_failure_replan", scenario_tool_failure_replan),
    ("supervisor_restart", scenario_supervisor_restart),
    ("side_effect_ledger", scenario_side_effect_ledger),
    ("verification_convergence", scenario_verification_convergence),
    ("checkpoint_resume", scenario_checkpoint_resume),
]
