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
from runtime.knowledge_reliability import KnowledgeRuntime, RetrievalPlan
from runtime.provider_reliability import ReliableProviderAdapter
from runtime.verification import VerificationEngine
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
            rows = result if isinstance(result, list) else (result.get("results", []) if isinstance(result, dict) else [])
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


__all__ = ["CanonicalWorkerAdapter"]
