"""Canonical production-seam driver for the formal P1 qualification run.

The timed workload enters through production-owned entry points only:

  g1_plan (project_run_goal G1, explicit tasks, no LLM)
    -> save_goal_run / load_goal_run (durable GoalRun persistence)
    -> CanonicalWorkerAdapter.for_capability + before_execution
       (binds PersistentComputer, ContextEngine, ActionGateway +
        SideEffectLedger, VerificationEngine, VerificationSpec)
    -> GoalRunHarnessAdapter.attach (the production LongRunningHarness owner)
    -> MasterCoordinator + bind_canonical_action_adapter
       (MasterAgentActionAdapter -> CanonicalActionRequest)
    -> per action: MasterAgent decision point
       (_guarded_handle_tool_call)
       -> CanonicalWorkerAdapter.execute_canonical_action (SAME GoalRun)
       -> ActionGateway.execute (approval + policy + audit + ledger)
       -> physical executor (real file-IO work)
    -> harness_adapter.observe / persist / worker.checkpoint
    -> verification convergence via worker.verification_engine +
       run_independent_verifier + harness apply_verification
    -> supervisor restart via load_goal_run + re-bind (same computer/GoalRun)

Acceptance never reads a component's isolated return value: every check
below consumes production-owned records (state.budget entries,
runtime_checkpoint entries, ledger rows, harness observations, verifier
verdicts, event stream).  Direct component construction for acceptance is
zero by construction — there is no other path in this module.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


class CanonicalRun:
    """Owns one formal run across the canonical seam.  Async build/run."""

    def __init__(self, ctx: dict):
        self.ctx = ctx
        self.collector = ctx["collector"]
        self.workload = ctx["workload"]
        self.run_root = Path(ctx["run_root"])
        self.project_root = self.run_root / "canonical_project"
        self.project_root.mkdir(parents=True, exist_ok=True)
        self.goal_text = (
            "p1q formal: traverse the canonical action seam for 30-60 minutes "
            "with real work, real approvals, real ledger, real restarts, and "
            "convergent verification"
        )
        self.state: Any = None
        self.worker: Any = None
        self.harness_adapter: Any = None
        self.coordinator: Any = None
        self.adapter: Any = None
        self.task_id = "p1q-soak-task"
        self.physical_invocations: dict[str, int] = {}
        self.committed_keys: set[str] = set()
        self.t0 = time.time()

    # ------------------------------------------------------------------
    # entry
    # ------------------------------------------------------------------

    async def build(self) -> CanonicalRun:
        from veya.platform import load as _platform_load

        _platform_load("obase")
        from server.coordinator_master import MasterCoordinator
        from server.goal_run.canonical_worker import (
            CanonicalWorkerAdapter,
            MasterAgentActionAdapter,
        )
        from server.goal_run.harness_adapter import GoalRunHarnessAdapter
        from server.goal_run.planner import g1_plan
        from server.goal_run.store import save_goal_run

        tasks = [
            {
                "id": self.task_id,
                "title": "p1q canonical soak",
                "instruction": self.goal_text,
                "acceptance": ["each seam action leaves a ledger row and an artifact"],
                "depends_on": [],
                "assignee": "hicode",
                "parallel": False,
            }
        ]
        budget = {
            "max_wall_s": 3600,
            "max_leaf_tasks": 4,
            "max_retries_per_task": 5,
            "max_tool_calls": 1000000,
            "max_replans": 1000,
        }
        state, _ = await g1_plan(
            interpretation=self.goal_text,
            assumptions=[],
            goal_text=self.goal_text,
            budget=budget,
            project_root=str(self.project_root),
            explicit_tasks=tasks,
        )
        self.state = state
        save_goal_run(state, str(self.project_root))

        self.worker = CanonicalWorkerAdapter.for_capability(
            task_id=self.task_id,
            objective=self.goal_text,
            capability=None,
            approval_resolver=self._auto_approve,
        )
        await self.worker.before_execution(state, str(self.project_root))
        self.collector.emit(
            "canonical_entry",
            goal_run_id=state.goal_id,
            computer_id=self.worker.computer_id,
            entry="g1_plan->before_execution->attach->bind",
        )

        self.harness_adapter = GoalRunHarnessAdapter.attach(state, str(self.project_root))

        self.coordinator = MasterCoordinator(max_rounds=2)
        self.adapter = MasterAgentActionAdapter(
            goal_run_id=state.goal_id,
            task_id=self.task_id,
            computer_ref=self.worker.computer_id,
            approval={"side_effect": "local_write", "effect_capability": "idempotency_key"},
            executor=lambda request: self.worker.execute_canonical_action(
                self.state, request, gateway_executor=self._physical
            ),
        )
        self.coordinator.bind_canonical_action_adapter(self.adapter)
        return self

    @staticmethod
    async def _auto_approve(_request: Any) -> bool:
        return True

    # ------------------------------------------------------------------
    # physical executor: real file-IO work per action
    # ------------------------------------------------------------------

    def _physical(self, request: Any) -> dict:
        args = dict(getattr(request, "arguments", {}) or {})
        cycle = int(args.get("cycle", -1))
        output = str(args.get("output", ""))
        key = f"{request.action_id}"
        self.physical_invocations[key] = self.physical_invocations.get(key, 0) + 1
        SeamGuard.require_open()
        target = self.project_root / "seam_artifacts" / f"{request.action_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "action_id": request.action_id,
            "goal_run_id": request.goal_run_id,
            "cycle": cycle,
            "output": output,
            "invocation": self.physical_invocations[key],
            "sha256": hashlib.sha256(f"{key}:{cycle}:{output}".encode()).hexdigest(),
        }
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        read_back = json.loads(target.read_text(encoding="utf-8"))
        assert read_back["sha256"] == payload["sha256"], "seam artifact read-back mismatch"
        return {
            "cycle": cycle,
            "output": output,
            "artifact": str(target.relative_to(self.project_root)),
        }

    # ------------------------------------------------------------------
    # one seam action through the MasterAgent decision point
    # ------------------------------------------------------------------

    async def seam_action(self, cycle: int, output: str) -> dict:
        from server.goal_run.store import save_goal_run

        args = {"cycle": cycle, "output": output}
        SeamGuard.open()
        try:
            result_str = await self.coordinator._guarded_handle_tool_call("p1q.work", args)
        finally:
            SeamGuard.close()
        self.collector.count("tool_executions")
        record = dict((self.state.budget or {}).get("last_canonical_action") or {})
        result = record.get("result") or {}
        # Attach the real artifact to the production task graph so the
        # production harness observes genuine progress (not a counter).
        task = self.state.tasks.get(self.task_id)
        artifact = (result.get("result") or {}) if isinstance(result, dict) else {}
        artifact_ref = artifact.get("artifact") if isinstance(artifact, dict) else None
        if task is not None and artifact_ref and artifact_ref not in task.artifacts:
            task.artifacts.append(artifact_ref)
        progressed = self.harness_adapter.observe(task_id=self.task_id)
        save_goal_run(self.state, str(self.project_root))
        return {"result_str": result_str, "record": record, "progressed": progressed}

    # ------------------------------------------------------------------
    # restart: real resume path (load + re-bind, same computer/GoalRun)
    # ------------------------------------------------------------------

    async def restart_supervisors(self, supervisor_a: str, supervisor_b: str) -> dict:
        from server.goal_run.canonical_worker import (
            CanonicalWorkerAdapter,
            MasterAgentActionAdapter,
        )
        from server.goal_run.store import load_goal_run, save_goal_run
        from server.goal_run.supervisor_restart import (
            claim_exclusive_session,
            restart_and_resume_canonical_action,
        )

        # Persist the harness projection BEFORE saving: load_goal_run must
        # restore the latest observations, otherwise the resume loses them.
        self.harness_adapter.persist(reason="pre_restart")
        save_goal_run(self.state, str(self.project_root))
        pre_obs = len(self.harness_adapter.harness.state.observations)
        computer_id = self.worker.computer_id
        goal_id = self.state.goal_id

        probe_request = self.adapter.request("p1q.probe", {"phase": "pre-restart"})
        claim_exclusive_session(
            self.worker.computer_store,
            computer_id=computer_id,
            owner_id=goal_id,
            supervisor_id=supervisor_a,
        )
        SeamGuard.open()
        try:
            await restart_and_resume_canonical_action(
                self.worker.computer_store,
                computer_id=computer_id,
                owner_id=goal_id,
                supervisor_a=supervisor_a,
                supervisor_b=supervisor_b,
                request=probe_request,
                executor=lambda req: self.worker.execute_canonical_action(
                    self.state, req, gateway_executor=self._physical
                ),
            )
        finally:
            SeamGuard.close()
        # Rebind through the real resume path: reload persisted GoalRun,
        # re-run production binding (same computer via goal link), continue.
        state2 = load_goal_run(str(self.project_root), goal_id)
        assert state2 is not None, "GoalRun must survive supervisor restart"
        worker_b = CanonicalWorkerAdapter.for_capability(
            task_id=self.task_id,
            objective=self.goal_text,
            capability=None,
            approval_resolver=self._auto_approve,
        )
        await worker_b.before_execution(state2, str(self.project_root))
        self.collector.count("restores")
        adapter_b = MasterAgentActionAdapter(
            goal_run_id=goal_id,
            task_id=self.task_id,
            computer_ref=worker_b.computer_id,
            approval={"side_effect": "local_write", "effect_capability": "idempotency_key"},
            executor=None,  # bound below (closure needs worker_b/state2)
        )

        async def _exec_b(request: Any) -> Any:
            return await worker_b.execute_canonical_action(
                state2, request, gateway_executor=self._physical
            )

        adapter_b.executor = _exec_b
        self.coordinator.bind_canonical_action_adapter(adapter_b)
        self.state = state2
        self.worker = worker_b
        self.adapter = adapter_b
        from server.goal_run.harness_adapter import GoalRunHarnessAdapter

        self.harness_adapter = GoalRunHarnessAdapter.attach(state2, str(self.project_root))
        resumed = {
            "same_computer": worker_b.computer_id == computer_id,
            "same_goalrun": state2.goal_id == goal_id,
            "observations_preserved": len(self.harness_adapter.harness.state.observations)
            >= pre_obs,
            "computer_id": computer_id,
        }
        self.collector.emit(
            "canonical_restart",
            **{k: v for k, v in resumed.items() if k != "computer_id"},
            computer_id=computer_id,
        )
        return resumed


class SeamGuard:
    """Harness-side gate: physical may run only inside a seam traversal."""

    _open = False

    @classmethod
    def open(cls) -> None:
        cls._open = True

    @classmethod
    def close(cls) -> None:
        cls._open = False

    @classmethod
    def require_open(cls) -> None:
        if not cls._open:
            raise RuntimeError("SECOND_EXECUTION_AUTHORITY: physical outside the canonical seam")
