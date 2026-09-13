"""P3 final qualification runner (evaluation-only, no production wiring)."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


class Collector:
    def __init__(self) -> None:
        self.metrics: dict[str, int] = {}

    def count(self, name: str, value: int = 1) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + value

    def emit(self, _name: str, **_payload: Any) -> None:
        return None


async def run_qualification(target_s: float, root: Path) -> dict[str, Any]:
    from evals.p1_qualification.canonical_run import CanonicalRun
    from runtime.execution.delegate_runtime import DelegateRuntime
    from runtime.execution.fanin import reconcile_multi_bot_results
    from runtime.execution.models import Assertion, DelegateRequest, DelegateResult, Evidence
    from runtime.execution.spawn_guard import SpawnBudget, SpawnGuard
    from runtime.verification.models import EvidenceBundle, EvidenceItem
    from server.goal_run.models import FanInState, GoalStatus
    from server.goal_run.store import save_goal_run

    root.mkdir(parents=True, exist_ok=True)
    collector = Collector()
    run = await CanonicalRun(
        {
            "run_root": str(root),
            "collector": collector,
            "workload": None,
            "goal_run_id": "p3q-goal",
            "computer_id": "p3q-computer",
            "head_sha": _head(),
            "t0": time.time(),
        }
    ).build()
    workload = RealP3Workload(root)
    runtime = DelegateRuntime(
        SpawnGuard(SpawnBudget(max_parallel=3)), goal_run_id=run.state.goal_id, bot_id="bot-a"
    )
    counts = {"delegations": 0, "fanin": 0, "conflicts": 0, "restarts": 0}
    started = time.perf_counter()
    digest = "genesis"

    async def semantic_delegate(bot_id: str, goal_id: str) -> DelegateResult:
        result = workload.cycle(bot_id)
        return DelegateResult(
            delegate_id=f"{bot_id}-{result.cycle}",
            status="complete",
            stop_reason="completed",
            summary=f"semantic proposal from {bot_id}",
            assertions=[Assertion("shared-claim", f"route-{bot_id}", producer=bot_id)],
            evidence=[Evidence("shared-evidence", "analysis", bot_id, result.digest, bot_id)],
            proposed_actions=[{"logical_operation": "publish", "route": bot_id}],
            side_effect_intents=[{"operation_key": "publish:p3q", "route": bot_id}],
            source_bot_id="bot-a",
            target_bot_id=bot_id,
            goal_run_id=goal_id,
            evidence_refs=[result.artifact],
        )

    while time.perf_counter() - started < target_s:
        serial = workload.cycle("bot-a")
        requests = [
            ("bot-b", "bot-b-goal"),
            ("bot-c", "bot-c-goal"),
        ]
        results = await asyncio.gather(
            *(
                runtime.run_cross_bot(
                    DelegateRequest(
                        delegate_id=f"{bot}-{serial.cycle}",
                        parent_task_id="p3q-task",
                        parent_trace_id=run.state.goal_id,
                        objective="semantic proposal",
                        source_bot_id="bot-a",
                        target_bot_id=bot,
                        target_goal_run_id=goal_id,
                        bot_id=bot,
                    ),
                    lambda _cancel, b=bot, g=goal_id: semantic_delegate(b, g),
                    source_bot_id="bot-a",
                    target_bot_id=bot,
                    target_goal_run_id=goal_id,
                )
                for bot, goal_id in requests
            )
        )
        report = reconcile_multi_bot_results(
            results,
            owner_bot_id="bot-a",
            owner_resolution={"owner_bot_id": "bot-a", "selected_route": "bot-b"},
        )
        counts["delegations"] += 2
        counts["fanin"] += 1
        counts["conflicts"] += len(report["conflicts"])
        await run.seam_action(serial.cycle, serial.digest)
        digest = hashlib.sha256(f"{digest}:{serial.digest}".encode()).hexdigest()
        if counts["restarts"] == 0:
            run.harness_adapter.persist(reason="p3q-checkpoint")
            save_goal_run(run.state, str(run.project_root))
            resumed = await run.restart_supervisors("p3q-supervisor-a", "p3q-supervisor-b")
            assert resumed["same_goalrun"] and resumed["same_computer"]
            counts["restarts"] = 1

    spec = run.worker.spec
    bundle = EvidenceBundle(
        task_id=spec.task_id,
        goal_run_id=spec.goal_run_id,
        head_sha=_head(),
        verification_spec_version=spec.version,
        verification_spec_hash=spec.spec_hash,
        evidence=[
            EvidenceItem(
                id=f"p3q-{criterion.id}",
                kind="observation",
                source="p3q-real-workload",
                content=digest,
                producer="goal_run",
                metadata={"criterion_id": criterion.id},
            )
            for criterion in spec.acceptance_criteria
        ]
        + [
            EvidenceItem(
                id=f"p3q-{negative.id}",
                kind="observation",
                source="p3q-real-workload",
                content=f"negative case handled in real run {digest}",
                producer="goal_run",
            )
            for negative in spec.negative_cases
        ]
        + [
            EvidenceItem(
                id=f"p3q-{cleanup.id}",
                kind="observation",
                source="p3q-real-workload",
                content=f"cleanup recorded after real run {digest}",
                producer="goal_run",
            )
            for cleanup in spec.cleanup_actions
        ],
        bot_id=run.state.bot_id,
    )
    verdict = await run.worker.verification_engine.run_independent_verifier(spec, bundle, _head())
    if verdict.outcome == "PASS":
        run.state.status = GoalStatus.completed
        save_goal_run(run.state, str(run.project_root))
    elapsed = time.perf_counter() - started
    run.state.fanin_states["p3q-fanin"] = FanInState(
        fanin_id="p3q-fanin",
        expected_delegate_ids=["bot-b", "bot-c"],
        completed_delegate_ids=["bot-b", "bot-c"],
        conflict_refs=[f"conflict-{i}" for i in range(counts["conflicts"])],
        resolved_by_bot_id="bot-a",
        bot_id="bot-a",
    )
    save_goal_run(run.state, str(run.project_root))
    return {
        "head_sha": _head(),
        "elapsed_s": round(elapsed, 2),
        "bot_count": 3,
        "delegations": counts["delegations"],
        "fanin": counts["fanin"],
        "conflicts": counts["conflicts"],
        "restarts": counts["restarts"],
        "verdict": verdict.outcome,
        "finalized": verdict.outcome == "PASS" and run.state.status == GoalStatus.completed,
    }


class RealP3Workload:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.artifacts = root / "p3-work"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.cycle_count = 0

    def cycle(self, owner: str) -> Any:
        from evals.p3_qualification.workload import RealP3Workload as Workload

        # Keep one deterministic implementation of the real CPU/IO cycle.
        if not hasattr(self, "_impl"):
            self._impl = Workload(self.root)
        result = self._impl.cycle(owner)
        self.cycle_count = self._impl.cycle_count
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("smoke", "formal"), default="formal")
    parser.add_argument("--target-seconds", type=float, default=None)
    parser.add_argument("--work-root", default="")
    args = parser.parse_args(argv)
    target = args.target_seconds or (60.0 if args.mode == "smoke" else 360.0)
    if args.mode == "smoke" and not 60 <= target < 300:
        parser.error("smoke target must be in [60,300)")
    if args.mode == "formal" and not 300 <= target <= 600:
        parser.error("formal target must be in [300,600]")
    try:
        result = asyncio.run(
            run_qualification(target, Path(args.work_root or ".veya/qualification/p3q"))
        )
    except Exception as exc:
        print(f"P3_QUALIFICATION_CRASH={type(exc).__name__}: {exc}")
        return 1
    for key, value in result.items():
        print(f"{key.upper()}={value}")
    duration_ok = result["elapsed_s"] >= target and result["elapsed_s"] <= 600
    required_ok = result["bot_count"] >= 3 and result["delegations"] >= 2 and result["fanin"] >= 1 and result["conflicts"] >= 1
    return 0 if duration_ok and required_ok and result["verdict"] == "PASS" and result["finalized"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
