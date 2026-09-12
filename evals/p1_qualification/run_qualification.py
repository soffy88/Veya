"""P1 qualification runner.

Modes:
  smoke  — the 7 controlled mechanics scenarios + short soak (HARNESS_MECHANICS).
  formal — canonical production-seam entry (g1_plan -> worker binding ->
           coordinator seam -> gateway -> ledger -> restart -> verifier),
           timed soak with scheduled fault injection, invariant monitors,
           REAL_* counters (FINAL qualification class).

Exit codes: 0 = completed with all gates green; 2 = completed with red
gates; 1 = crashed (see failure_trace.json).  PASS verdicts are recorded
per-gate in the report, never printed as an overall claim.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evals.p1_qualification.assertions import run_all
from evals.p1_qualification.collector import EventCollector
from evals.p1_qualification.report import write_report
from evals.p1_qualification.scenarios import SCENARIOS
from evals.p1_qualification.workload import RealWorkload

SOAK_CHECKPOINT_EVERY = 25
SOAK_PROVIDER_EVERY = 100
SOAK_CONTEXT_EVERY = 200


def _head_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _run_soak(ctx: dict, target_s: float) -> None:
    """Fill the clock to target_s with real cycles + subsystem traffic."""
    from evals.p1_qualification.scenarios import _extractive_summary
    from runtime.context.engine import ContextEngine
    from runtime.context.models import ContextBudget, ContextLayer, PreservedItems
    from runtime.execution.long_running import (
        LongRunBudget,
        LongRunCheckpointStore,
        LongRunningHarness,
        LongRunState,
        ProgressObservation,
    )
    from runtime.provider_reliability import ReliableProviderAdapter

    collector = ctx["collector"]
    workload = ctx["workload"]
    t0 = ctx["t0"]

    soak_store = LongRunCheckpointStore(Path(ctx["run_root"]) / "soak")
    soak_harness = LongRunningHarness(
        LongRunState(goal_run_id=ctx["goal_run_id"], computer_id=ctx["computer_id"], plan=["soak"]),
        LongRunBudget(
            max_wall_s=3600,
            max_tool_calls=10_000_000,
            max_retrievals=10_000_000,
            max_context_tokens=10_000_000,
            max_replans=10_000,
        ),
        checkpoint_store=soak_store,
    )
    soak_engine = ContextEngine(
        ctx["goal_run_id"],
        ctx["computer_id"],
        budget=ContextBudget(max_tokens=60000, trigger_ratio=0.6),
        llm_summarizer=_extractive_summary,
    )
    soak_engine.set_preserved(
        PreservedItems(
            objective="p1q soak: keep refs across the long run",
            computer_id=ctx["computer_id"],
            goal_run_id=ctx["goal_run_id"],
        )
    )
    soak_adapter = ReliableProviderAdapter(provider_router=None, failure_threshold=2)

    import asyncio

    cycles = 0
    while time.time() - t0 < target_s:
        ev = workload.cycle()
        cycles += 1
        collector.count("work_cycles")
        if cycles % SOAK_CHECKPOINT_EVERY == 0:
            soak_harness.record_progress(
                ProgressObservation(
                    artifacts=[ev.artifact_relpath],
                    state_hash=ev.output_hash,
                    tool_result_hash=ev.output_hash,
                    plan_step="soak",
                )
            )
            soak_harness.checkpoint(reason="soak_periodic")
            collector.count("checkpoints")
        if cycles % SOAK_PROVIDER_EVERY == 0:
            current = ev

            async def _call(frozen=current):
                return await soak_adapter.call(
                    lambda _p, _e=frozen: _aresult(_e),
                    ["secondary-p1q"],
                    goal_run_id=ctx["goal_run_id"],
                    context={"goal_run_id": ctx["goal_run_id"]},
                )

            asyncio.run(_call())
            collector.count("provider_calls")
        if cycles % SOAK_CONTEXT_EVERY == 0:
            soak_engine.append_to_layer(
                ContextLayer.L2_OBSERVATIONS,
                [{"cycle": ev.cycle, "output": ev.output_hash}],
                token_estimate=220,
            )
            collector.count("context_appends")
            if soak_engine.should_compact():
                plan = soak_engine.build_compaction_plan()
                soak_engine.execute_compaction(plan, llm_summarizer=_extractive_summary)
                collector.count("compactions")
        if cycles % 50 == 0:
            collector.emit("soak_progress", cycles=cycles, elapsed=round(time.time() - t0, 1))
    collector.emit("soak_done", cycles=cycles, elapsed=round(time.time() - t0, 1))


async def _aresult(ev):
    return {"ok": True, "output": ev.output_hash, "schema": "p1q-result-1"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P1 qualification harness runner")
    ap.add_argument("--target-seconds", type=float, default=1800.0)
    ap.add_argument("--work-root", type=str, default="")
    ap.add_argument("--head-sha", type=str, default="")
    ap.add_argument("--mode", type=str, default="formal", choices=("smoke", "formal"))
    args = ap.parse_args(argv)

    target_s = float(args.target_seconds)
    if not 60 <= target_s <= 3900:
        print(f"refusing target {target_s}: must be within [60, 3900]", flush=True)
        return 1

    ts = time.strftime("%Y%m%d-%H%M%S")
    run_root = (
        Path(args.work_root) if args.work_root else Path(".veya") / "qualification" / f"p1q-{ts}"
    )
    run_root.mkdir(parents=True, exist_ok=True)
    head_sha = args.head_sha or _head_sha()
    goal_run_id = f"p1q-goal-{ts}"
    computer_id = f"p1q-computer-{ts}"

    collector = EventCollector(run_root)
    workload = RealWorkload(run_root)
    calibration = workload.calibrate()
    t0 = time.time()
    ctx = {
        "collector": collector,
        "workload": workload,
        "run_root": str(run_root),
        "goal_run_id": goal_run_id,
        "computer_id": computer_id,
        "head_sha": head_sha,
        "t0": t0,
    }
    collector.emit(
        "harness_started",
        head_sha=head_sha,
        target_s=target_s,
        goal_run_id=goal_run_id,
        computer_id=computer_id,
        calibration=calibration,
    )
    print(f"[p1q] run_dir={run_root} target={target_s:.0f}s calibration={calibration}", flush=True)

    results: dict = {}
    try:
        if args.mode == "formal":
            return _run_formal(
                ctx, collector, workload, run_root, head_sha, target_s, calibration, t0
            )
        for name, fn in SCENARIOS:
            collector.emit("scenario_started", scenario=name)
            started = time.time()
            results[name] = fn(ctx)
            collector.scenario(name, "completed", duration_s=round(time.time() - started, 1))
            print(f"[p1q] scenario {name} done ({time.time() - t0:.0f}s elapsed)", flush=True)
        _run_soak(ctx, target_s)
        elapsed = time.time() - t0
        collector.flush_metrics()
        checks = run_all(results, collector.metrics, elapsed, target_s)
        write_report(
            run_root,
            head_sha=head_sha,
            target_s=target_s,
            elapsed_s=elapsed,
            results=results,
            checks=checks,
            metrics={**collector.metrics, "elapsed_s": round(elapsed, 1), "events": collector._seq},
            calibration=calibration,
        )
        failed = [c["name"] for c in checks if not c["passed"]]
        print(
            f"[p1q] HARNESS_COMPLETED elapsed={elapsed:.0f}s "
            f"checks={len(checks) - len(failed)}/{len(checks)}",
            flush=True,
        )
        if failed:
            print(f"[p1q] red checks: {failed} (NOT a qualification verdict)", flush=True)
            return 2
        print(
            "[p1q] all checks green — still NOT a P1 qualification PASS "
            "(requires I4 PASS + human review)",
            flush=True,
        )
        return 0
    except Exception as exc:  # preserve everything, then exit nonzero
        trace_path = collector.write_failure_trace(exc, phase="runner")
        collector.flush_metrics()
        print(f"[p1q] HARNESS_CRASHED: {exc!r} trace={trace_path}", flush=True)
        return 1


def _run_formal(ctx, collector, workload, run_root, head_sha, target_s, calibration, t0) -> int:
    import asyncio

    from evals.p1_qualification import formal_checks
    from evals.p1_qualification.formal_run import run_formal
    from evals.p1_qualification.report import write_formal_report

    print(f"[p1q] formal canonical entry (target={target_s:.0f}s)", flush=True)
    outcome = asyncio.run(run_formal(ctx, target_s))
    elapsed = outcome["elapsed"]
    fst = outcome["state"]
    collector.flush_metrics()
    checks = formal_checks.build(outcome, elapsed, target_s)
    metrics = {
        **collector.metrics,
        "elapsed_s": round(elapsed, 1),
        "events": collector._seq,
        "real_provider_calls": fst.get("provider_calls", 0)
        + collector.metrics.get("provider_calls", 0),
        "real_tool_executions": fst.get("actions", 0),
        "real_context_cycles": fst.get("context_cycles", 0),
        "real_restarts": fst.get("restarts", 0),
        "real_checkpoints": fst.get("checkpoints", 0),
    }
    write_formal_report(
        run_root,
        head_sha=head_sha,
        target_s=target_s,
        elapsed_s=elapsed,
        outcome=outcome,
        checks=checks,
        metrics=metrics,
        calibration=calibration,
    )
    failed = [c["name"] for c in checks if not c["passed"]]
    print(
        f"[p1q] FORMAL_COMPLETED elapsed={elapsed:.0f}s "
        f"checks={len(checks) - len(failed)}/{len(checks)}",
        flush=True,
    )
    if failed:
        print(f"[p1q] red checks: {failed}", flush=True)
        return 2
    print("[p1q] formal gates green (per-gate verdicts in report)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
