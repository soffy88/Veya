"""Compatibility wiring for the canonical GoalRun worker.

This module deliberately contains no scheduler or task state machine.  It is a
small lifecycle adapter used by ``project_run_goal`` to bind the already
existing P1 components to one GoalRun.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime.computer import PersistentComputerStore
from runtime.context import ContextEngine
from runtime.execution.artifacts import ArtifactStore
from runtime.execution.durable import DurableExecutionRepository
from runtime.execution.side_effects import SideEffectLedger
from runtime.knowledge_reliability import KnowledgeRuntime, RetrievalPlan
from runtime.provider_reliability import ReliableProviderAdapter
from runtime.verification import EvidenceBundle, EvidenceItem, VerificationEngine
from server.action_gateway_adapter import ActionGatewayAdapter
from server.goal_run.action_protocol import CanonicalActionRequest, CanonicalActionResult
from server.goal_run.git_diff import current_head


class CanonicalWorkerAdapter:
    """Bind frozen runtimes to one GoalRun without owning execution authority."""

    def __init__(
        self,
        *,
        task_id: str,
        objective: str,
        feature_name: str = "veya_cli",
        verification_engine: VerificationEngine | None = None,
        provider_adapter: ReliableProviderAdapter | None = None,
        provider_router: Any | None = None,
        knowledge_plan: RetrievalPlan | None = None,
        retriever: Any | None = None,
        provider_request: Any | None = None,
        provider_candidates: list[str] | None = None,
        verification_required: bool = False,
        approval_resolver: Any | None = None,
        policy_hook: Any | None = None,
    ) -> None:
        self.task_id = task_id
        self.objective = objective
        self.feature_name = feature_name
        self.verification_engine = verification_engine
        self.provider_adapter = provider_adapter or ReliableProviderAdapter(provider_router)
        self.knowledge_plan = knowledge_plan
        self.knowledge_runtime: KnowledgeRuntime | None = None
        self.retriever = retriever
        self.provider_request = provider_request
        self.provider_candidates = list(provider_candidates or [])
        self.knowledge_result: Any | None = None
        self.provider_response: Any | None = None
        self.computer_store: PersistentComputerStore | None = None
        self.context_engine: ContextEngine | None = None
        self.spec: Any | None = None
        self._spec_hash: str | None = None
        self.computer_id: str | None = None
        self._checkpoint_path: Path | None = None
        self.verification_required = verification_required
        self.evidence_bundle: EvidenceBundle | None = None
        self.verdict: Any | None = None
        self.approval_resolver = approval_resolver
        self.policy_hook = policy_hook
        self.execution_repository: DurableExecutionRepository | None = None
        self.action_gateway: ActionGatewayAdapter | None = None

    async def execute_canonical_action(
        self,
        state: Any,
        request: CanonicalActionRequest,
        *,
        gateway_executor: Any,
    ) -> CanonicalActionResult:
        """Execute one semantic action through the already-bound GoalRun.

        The physical step always crosses the existing ActionGateway boundary
        (approval + policy + audit + SideEffectLedger); the ledger's stable
        operation key makes a resumed/repeated action hit the committed row
        instead of executing twice.  This method adapts the gateway result
        into the cross-layer ABI only: no loop, no retry, no acceptance
        decision — those authorities stay with GoalRun and Verification OS.
        """
        if request.goal_run_id != state.goal_id:
            raise ValueError("canonical action belongs to a different GoalRun")
        if self.computer_id and request.computer_ref not in {None, self.computer_id}:
            raise ValueError("canonical action targets a different PersistentComputer")

        state.budget["pending_canonical_action"] = request.to_dict()
        state.runtime_checkpoint = {
            **(state.runtime_checkpoint or {}),
            "canonical_action": {
                "action_id": request.action_id,
                "goal_run_id": request.goal_run_id,
                "computer_id": request.computer_ref or self.computer_id,
                "idempotency_key": request.idempotency_key,
                "status": "pending",
            },
        }

        try:
            if self.action_gateway is None:
                raise RuntimeError("GoalRun ActionGateway is not bound")

            async def physical(**_arguments: Any) -> Any:
                value = gateway_executor(request)
                if hasattr(value, "__await__"):
                    return await value
                return value

            result = self.action_gateway.execute(
                request.tool,
                request.arguments,
                physical,
                side_effect=request.approval.get("side_effect"),
                effect_capability=str(request.approval.get("effect_capability") or "manual_only"),
                resource=str(request.approval.get("resource") or request.tool),
                source="goal_run_canonical_action",
                request_context={
                    "goal_run_id": state.goal_id,
                    "computer_id": self.computer_id,
                    "context_ref": request.context_ref,
                },
            )
            if hasattr(result, "__await__"):
                result = await result
        except Exception as exc:
            return CanonicalActionResult(
                action_id=request.action_id,
                status="failed",
                attempted=True,
                executed=False,
                failure_evidence=(
                    {
                        "stage": "physical_execution",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                ),
            )

        if isinstance(result, CanonicalActionResult):
            action_result = result
        elif isinstance(result, dict):
            status = str(result.get("status") or "completed")
            nested = result.get("result")
            payload = (
                nested
                if isinstance(nested, dict)
                and ("status" in nested or "error" in nested or "result" in nested)
                else result
            )
            raw_failure = result.get("failure_evidence")
            if isinstance(raw_failure, dict):
                failure_evidence = (raw_failure,)
            elif raw_failure:
                failure_evidence = tuple(dict(item) for item in raw_failure)
            elif status != "completed":
                failure_evidence = (
                    {
                        "stage": "physical_execution"
                        if isinstance(payload, dict) and payload.get("error")
                        else "action_gateway",
                        "error": result.get("error") or payload.get("error"),
                    },
                )
            else:
                failure_evidence = ()
            action_result = CanonicalActionResult(
                action_id=request.action_id,
                status=status,
                attempted=bool(result.get("attempted", True)),
                executed=bool(result.get("executed", status == "completed")),
                result=payload.get("result", payload) if isinstance(payload, dict) else payload,
                failure_evidence=failure_evidence,
                artifact_refs=tuple(str(item) for item in result.get("artifact_refs") or ()),
                evidence_refs=tuple(str(item) for item in result.get("evidence_refs") or ()),
                approval=dict(result.get("approval") or {}),
            )
        else:
            action_result = CanonicalActionResult(
                action_id=request.action_id,
                status="completed",
                attempted=True,
                executed=True,
                result=result,
            )
        state.budget["last_canonical_action"] = {
            "request": request.to_dict(),
            "result": action_result.to_dict(),
        }
        state.budget.pop("pending_canonical_action", None)
        state.runtime_checkpoint["canonical_action"]["status"] = action_result.status
        return action_result

    @classmethod
    def for_capability(
        cls,
        *,
        task_id: str,
        objective: str,
        capability: str | None,
        **kwargs: Any,
    ) -> CanonicalWorkerAdapter:
        """Build the integration projection from the semantic capability.

        Capability selection remains MasterAgent's responsibility.  This
        method only resolves the already-selected knowledge dependency at the
        GoalRun boundary; it never starts another runner.
        """
        if capability not in {"knowledge", "research"}:
            return cls(task_id=task_id, objective=objective, **kwargs)

        plan = kwargs.pop("knowledge_plan", None) or RetrievalPlan(
            question=objective,
            required_evidence=[],
            source_types=["knowledge"],
            depth=1,
            retrieval_budget=5,
        )
        retriever = kwargs.pop("retriever", None) or cls._canonical_retriever()
        return cls(
            task_id=task_id,
            objective=objective,
            knowledge_plan=plan,
            retriever=retriever,
            **kwargs,
        )

    @staticmethod
    def _canonical_retriever() -> Any:
        """Adapt the existing Stratum knowledge tool to KnowledgeRuntime."""
        from runtime.knowledge_reliability import Evidence
        from server.stratum_memory import get_stratum

        async def retrieve(query: str, source_type: str, depth: int) -> list[Evidence]:
            result = await get_stratum().search_knowledge(query=query, top_k=max(5, depth * 5))
            rows = (
                result
                if isinstance(result, list)
                else (result.get("results", []) if isinstance(result, dict) else [])
            )
            evidence: list[Evidence] = []
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                content = str(row.get("content") or row.get("text") or row.get("snippet") or "")
                source = str(row.get("source") or row.get("url") or row.get("id") or "stratum")
                if not content:
                    continue
                evidence.append(
                    Evidence(
                        evidence_id=f"stratum:{source}:{index}",
                        query=query,
                        source=source,
                        content=content,
                        claim=str(row.get("claim") or content[:200]),
                        authority=float(row.get("authority", 0.7)),
                        relevance=float(row.get("relevance", 0.7)),
                        source_type=source_type,
                    )
                )
            return evidence

        return retrieve

    async def before_execution(self, state: Any, project_root: str) -> None:
        """Freeze acceptance and bind durable physical/context projections."""
        root = Path(project_root).expanduser().resolve()
        run_dir = root / ".veya" / "runs" / state.goal_id
        run_dir.mkdir(parents=True, exist_ok=True)

        if self.verification_engine is None:
            self.verification_engine = VerificationEngine(root)
        spec_path = root / ".veya" / "runs" / self.task_id / "verification_spec.json"
        if spec_path.exists():
            from runtime.verification.models import VerificationSpec

            self.spec = VerificationSpec.load(spec_path)
            if self.spec.goal_run_id != state.goal_id:
                raise RuntimeError("verification spec is bound to a different GoalRun")
        else:
            self.spec = await self.verification_engine.generate_verification_spec(
                self.task_id,
                state.goal_id,
                current_head(root),
                feature_name=self.feature_name,
            )

        db_path = root / ".veya" / "persistent_computers.sqlite3"
        self.computer_store = PersistentComputerStore(db_path)
        existing = self.computer_store.get_computer_for_goal_run(state.goal_id)
        computer = existing or self.computer_store.create_computer(
            owner_id=self.task_id,
            workspace_ref=str(root),
            browser_profile_ref=f"profile:{self.task_id}",
            downloads_ref=str(root / ".veya" / "runs" / self.task_id / "downloads"),
            computer_id=f"computer:{self.task_id}",
        )
        if existing is None:
            self.computer_store.link_goal_run(state.goal_id, computer.computer_id, self.task_id)
        self.computer_id = computer.computer_id
        self._spec_hash = getattr(self.spec, "spec_hash", None)
        state.budget["computer_id"] = self.computer_id
        state.budget["verification_spec_path"] = str(spec_path)

        if (run_dir / "context_checkpoint.json").exists():
            self.context_engine = ContextEngine.load_checkpoint(
                run_dir / "context_checkpoint.json",
                persistent_computer_store=self.computer_store,
            )
            if self.context_engine.state.goal_run_id != state.goal_id:
                raise RuntimeError("context checkpoint is bound to a different GoalRun")
        else:
            self.context_engine = ContextEngine(
                goal_run_id=state.goal_id,
                computer_id=self.computer_id,
                persistent_computer_store=self.computer_store,
            )
        self.context_engine.update_preserved_objective(self.objective)
        self.context_engine.update_preserved_verification_spec(str(spec_path))
        self.context_engine.update_preserved_plan(
            list(state.tasks),
            current_step=next(iter(state.tasks), ""),
        )
        self.context_engine.state.preserved = self.context_engine.state.preserved.__class__(
            **{
                **self.context_engine.state.preserved.to_dict(),
                "computer_id": self.computer_id,
                "goal_run_id": state.goal_id,
            }
        )
        self._checkpoint_path = run_dir / "context_checkpoint.json"
        self.execution_repository = DurableExecutionRepository(
            sqlite_path=root / ".veya" / "execution-runtime.sqlite3"
        )
        await self.execution_repository.connect()
        self.action_gateway = ActionGatewayAdapter(
            ledger=SideEffectLedger(self.execution_repository),
            goal_run_id=state.goal_id,
            work_item_id=self.task_id,
            approval_resolver=self.approval_resolver,
            policy_hook=self.policy_hook,
            output_dir=root / ".veya" / "action_gateway",
        )
        if self.knowledge_plan is not None:
            self.knowledge_runtime = KnowledgeRuntime(self.knowledge_plan)
        self.checkpoint(state, project_root, reason="before_first_action")

    async def before_iteration(self, state: Any, project_root: str, task: Any) -> None:
        if self.spec is not None and not self.spec.verify_immutable():
            raise RuntimeError("worker attempted to mutate frozen VerificationSpec")
        if self.knowledge_runtime is not None and self.retriever is not None:
            self.knowledge_result = await self.knowledge_runtime.retrieve(self.retriever)
            for ref in self.knowledge_result.evidence_refs:
                self.context_engine.add_evidence_ref(ref)
            state.budget["knowledge_evidence_refs"] = list(self.knowledge_result.evidence_refs)
        if self.provider_request is not None:
            if not self.provider_candidates:
                raise RuntimeError("provider candidates are required for canonical failover")
            provider, result, continuity = await self.provider_adapter.call(
                self.provider_request,
                self.provider_candidates,
                goal_run_id=state.goal_id,
                context=self.provider_context(state),
            )
            # This is the response consumed by the canonical iteration.  Keep
            # it in the same durable context envelope as the provider choice;
            # failover is therefore continuation, not a side-channel probe.
            self.provider_response = result
            state.budget["provider"] = provider
            state.budget["provider_continuity"] = continuity
            state.budget["provider_response"] = result
            if self.context_engine is not None:
                self.context_engine.add_important_observation(
                    {"provider": provider, "model_response": result, "goal_run_id": state.goal_id}
                )
        if self.context_engine is None:
            return
        self.context_engine.add_important_observation(
            {"task_id": task.id, "status": task.status.value, "goal_run_id": state.goal_id}
        )
        self.checkpoint(state, project_root, reason=f"iteration:{task.id}")

    async def finalize_candidate(self, state: Any, project_root: str) -> Any | None:
        """Gate candidate completion through the existing Verification OS."""
        if not self.verification_required:
            return None
        if self.verification_engine is None or self.spec is None:
            raise RuntimeError("verification engine/spec missing before finalize")
        if not self.spec.verify_immutable():
            raise RuntimeError("VerificationSpec changed before finalize")
        head_sha = current_head(project_root)
        bundle = await self.verification_engine.collect_evidence_bundle(
            self.task_id,
            state.goal_id,
            head_sha,
            self.spec,
            artifact_store=ArtifactStore(project_root, state.goal_id),
        )
        for task in state.tasks.values():
            for index, evidence in enumerate(task.evidence):
                bundle = bundle.add_evidence(
                    EvidenceItem(
                        id=f"goalrun-{task.id}-{index}",
                        kind="observation",
                        source=f"goal_run.task.{task.id}",
                        content=str(evidence),
                        producer="goal_run",
                    )
                )
        self.evidence_bundle = bundle
        self.verdict = await self.verification_engine.run_independent_verifier(
            self.spec, bundle, head_sha
        )
        state.budget["evidence_bundle"] = bundle.to_dict()
        state.budget["acceptance_verdict"] = self.verdict.to_dict()
        return self.verdict

    def checkpoint(self, state: Any, project_root: str, *, reason: str) -> None:
        if self.context_engine is None or self._checkpoint_path is None:
            return
        self.context_engine.save_checkpoint(self._checkpoint_path)
        state.runtime_checkpoint = {
            **(state.runtime_checkpoint or {}),
            "canonical_worker": {
                "goal_run_id": state.goal_id,
                "computer_id": self.computer_id,
                "verification_spec_path": state.budget.get("verification_spec_path"),
                "context_checkpoint": str(self._checkpoint_path),
                "reason": reason,
            },
        }

    def provider_context(self, state: Any) -> dict[str, Any]:
        """Continuity envelope for the existing provider adapter."""
        return {
            "goal_run_id": state.goal_id,
            "computer_id": self.computer_id,
            "verification_spec": state.budget.get("verification_spec_path"),
            "context_checkpoint": str(self._checkpoint_path) if self._checkpoint_path else None,
        }


class MasterAgentActionAdapter:
    """Turn one MasterAgent tool decision into the GoalRun ABI.

    The adapter builds the immutable request (deterministic action_id, so a
    repeated decision carries the same idempotency key) and hands it to the
    GoalRun-owned executor.  It performs no physical execution itself:
    MasterAgent never touches a tool boundary except through this adapter.
    """

    def __init__(
        self,
        *,
        goal_run_id: str,
        task_id: str,
        computer_ref: str | None = None,
        context_ref: str | None = None,
        approval: dict[str, Any] | None = None,
        executor: Any,
    ) -> None:
        self.goal_run_id = goal_run_id
        self.task_id = task_id
        self.computer_ref = computer_ref
        self.context_ref = context_ref
        self.approval = dict(approval or {})
        self.executor = executor

    def request(self, tool: str, arguments: dict[str, Any]) -> CanonicalActionRequest:
        import hashlib
        import json

        encoded = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
        action_id = hashlib.sha256(f"{tool}\0{encoded}".encode()).hexdigest()[:24]
        return CanonicalActionRequest(
            action_id=action_id,
            goal_run_id=self.goal_run_id,
            task_id=self.task_id,
            tool=tool,
            arguments=dict(arguments),
            capability=tool,
            computer_ref=self.computer_ref,
            context_ref=self.context_ref,
            approval=self.approval,
            idempotency_key=f"{self.goal_run_id}:{action_id}",
        )

    async def execute(self, tool: str, arguments: dict[str, Any]) -> CanonicalActionResult:
        request = self.request(tool, arguments)
        result = self.executor(request)
        if hasattr(result, "__await__"):
            result = await result
        if not isinstance(result, CanonicalActionResult):
            raise TypeError("GoalRun action executor must return CanonicalActionResult")
        return result


__all__ = ["CanonicalWorkerAdapter", "MasterAgentActionAdapter"]
