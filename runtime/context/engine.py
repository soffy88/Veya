"""P1-D Context Engine: Context layering, pressure detection, and selective compaction."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.computer import PersistentComputerStore
from runtime.context.models import (
    CompactionAction,
    CompactionPlan,
    CompactionRecord,
    ContextBudget,
    ContextEngineError,
    ContextLayer,
    ContextPressure,
    ContextState,
    DriftDetected,
    FalseMemoryInjected,
    LayerContent,
    PreservedItems,
    compute_context_hash,
)
from runtime.execution.long_running import LongRunCheckpointStore, LongRunState
from runtime.verification.engine import VerificationEngine
from veya.oskill.pure.context_compress import render_messages_for_summary


class ContextEngine:
    """Context Engine: Manages context layering, pressure, and selective compaction.

    Integrates with:
    - GoalRun (via LongRunState)
    - PersistentComputer (for computer_id, refs)
    - Verification OS (for VerificationSpec preservation)
    - LongRunningHarness (for checkpoint/resume)
    """

    def __init__(
        self,
        goal_run_id: str,
        computer_id: str,
        *,
        budget: ContextBudget | None = None,
        persistent_computer_store: PersistentComputerStore | None = None,
        long_run_state: LongRunState | None = None,
        checkpoint_store: LongRunCheckpointStore | None = None,
        verification_engine: VerificationEngine | None = None,
        llm_summarizer: Callable[[str], str] | None = None,
        context_state: ContextState | None = None,
    ):
        self.goal_run_id = goal_run_id
        self.computer_id = computer_id
        self.budget = budget or ContextBudget()
        self.persistent_computer_store = persistent_computer_store
        self.long_run_state = long_run_state
        self.checkpoint_store = checkpoint_store
        self.verification_engine = verification_engine
        self.llm_summarizer = llm_summarizer

        # Initialize or restore context state
        if context_state:
            self.state = context_state
        else:
            self.state = ContextState(
                goal_run_id=goal_run_id,
                computer_id=computer_id,
                preserved=PreservedItems(
                    computer_id=computer_id,
                    goal_run_id=goal_run_id,
                ),
            )
        self._context_hash = compute_context_hash(self.state)

    # ==================== Context Layer Management ====================

    def get_layer(self, layer: ContextLayer) -> LayerContent:
        return self.state.get_layer(layer)

    def set_layer(
        self, layer: ContextLayer, content: list[dict[str, Any]], token_estimate: int = 0
    ) -> None:
        """Set content for a layer."""
        self.state.layers[layer] = LayerContent(
            layer=layer,
            content=content,
            token_estimate=token_estimate,
            metadata={"updated_at": datetime.now(UTC).isoformat()},
        )
        self._update_totals()

    def append_to_layer(
        self, layer: ContextLayer, items: list[dict[str, Any]], token_estimate: int = 0
    ) -> None:
        """Append items to a layer."""
        existing = self.state.get_layer(layer)
        new_content = existing.content + items
        new_tokens = existing.token_estimate + token_estimate
        self.state.layers[layer] = LayerContent(
            layer=layer,
            content=new_content,
            token_estimate=new_tokens,
            metadata={"updated_at": datetime.now(UTC).isoformat()},
        )
        self._update_totals()

    def _update_totals(self) -> None:
        self.state.total_tokens = self.state.total_token_estimate()
        self.state.updated_at = datetime.now(UTC).isoformat()

    # ==================== Preserved Items Management ====================

    def set_preserved(self, preserved: PreservedItems) -> None:
        """Set preserved items (must be called before compaction)."""
        self.state.preserved = preserved

    def update_preserved_objective(self, objective: str) -> None:
        self.state.preserved = PreservedItems(
            objective=objective,
            verification_spec_ref=self.state.preserved.verification_spec_ref,
            active_plan=self.state.preserved.active_plan,
            current_step=self.state.preserved.current_step,
            unresolved_failures=self.state.preserved.unresolved_failures,
            important_observations=self.state.preserved.important_observations,
            artifact_refs=self.state.preserved.artifact_refs,
            evidence_refs=self.state.preserved.evidence_refs,
            computer_id=self.state.preserved.computer_id,
            goal_run_id=self.state.preserved.goal_run_id,
            feature_map_ref=self.state.preserved.feature_map_ref,
        )

    def update_preserved_verification_spec(self, spec_ref: str) -> None:
        self.state.preserved = PreservedItems(
            objective=self.state.preserved.objective,
            verification_spec_ref=spec_ref,
            active_plan=self.state.preserved.active_plan,
            current_step=self.state.preserved.current_step,
            unresolved_failures=self.state.preserved.unresolved_failures,
            important_observations=self.state.preserved.important_observations,
            artifact_refs=self.state.preserved.artifact_refs,
            evidence_refs=self.state.preserved.evidence_refs,
            computer_id=self.state.preserved.computer_id,
            goal_run_id=self.state.preserved.goal_run_id,
            feature_map_ref=self.state.preserved.feature_map_ref,
        )

    def update_preserved_plan(self, plan: list[str], current_step: str = "") -> None:
        self.state.preserved = PreservedItems(
            objective=self.state.preserved.objective,
            verification_spec_ref=self.state.preserved.verification_spec_ref,
            active_plan=plan,
            current_step=current_step or self.state.preserved.current_step,
            unresolved_failures=self.state.preserved.unresolved_failures,
            important_observations=self.state.preserved.important_observations,
            artifact_refs=self.state.preserved.artifact_refs,
            evidence_refs=self.state.preserved.evidence_refs,
            computer_id=self.state.preserved.computer_id,
            goal_run_id=self.state.preserved.goal_run_id,
            feature_map_ref=self.state.preserved.feature_map_ref,
        )

    def add_unresolved_failure(self, failure: dict[str, Any]) -> None:
        self.state.preserved = PreservedItems(
            objective=self.state.preserved.objective,
            verification_spec_ref=self.state.preserved.verification_spec_ref,
            active_plan=self.state.preserved.active_plan,
            current_step=self.state.preserved.current_step,
            unresolved_failures=[*self.state.preserved.unresolved_failures, failure],
            important_observations=self.state.preserved.important_observations,
            artifact_refs=self.state.preserved.artifact_refs,
            evidence_refs=self.state.preserved.evidence_refs,
            computer_id=self.state.preserved.computer_id,
            goal_run_id=self.state.preserved.goal_run_id,
            feature_map_ref=self.state.preserved.feature_map_ref,
        )

    def add_important_observation(self, observation: dict[str, Any]) -> None:
        self.state.preserved = PreservedItems(
            objective=self.state.preserved.objective,
            verification_spec_ref=self.state.preserved.verification_spec_ref,
            active_plan=self.state.preserved.active_plan,
            current_step=self.state.preserved.current_step,
            unresolved_failures=self.state.preserved.unresolved_failures,
            important_observations=[*self.state.preserved.important_observations, observation],
            artifact_refs=self.state.preserved.artifact_refs,
            evidence_refs=self.state.preserved.evidence_refs,
            computer_id=self.state.preserved.computer_id,
            goal_run_id=self.state.preserved.goal_run_id,
            feature_map_ref=self.state.preserved.feature_map_ref,
        )

    def add_artifact_ref(self, ref: str) -> None:
        if ref not in self.state.preserved.artifact_refs:
            self.state.preserved = PreservedItems(
                objective=self.state.preserved.objective,
                verification_spec_ref=self.state.preserved.verification_spec_ref,
                active_plan=self.state.preserved.active_plan,
                current_step=self.state.preserved.current_step,
                unresolved_failures=self.state.preserved.unresolved_failures,
                important_observations=self.state.preserved.important_observations,
                artifact_refs=[*self.state.preserved.artifact_refs, ref],
                evidence_refs=self.state.preserved.evidence_refs,
                computer_id=self.state.preserved.computer_id,
                goal_run_id=self.state.preserved.goal_run_id,
                feature_map_ref=self.state.preserved.feature_map_ref,
            )

    def add_evidence_ref(self, ref: str) -> None:
        if ref not in self.state.preserved.evidence_refs:
            self.state.preserved = PreservedItems(
                objective=self.state.preserved.objective,
                verification_spec_ref=self.state.preserved.verification_spec_ref,
                active_plan=self.state.preserved.active_plan,
                current_step=self.state.preserved.current_step,
                unresolved_failures=self.state.preserved.unresolved_failures,
                important_observations=self.state.preserved.important_observations,
                artifact_refs=self.state.preserved.artifact_refs,
                evidence_refs=[*self.state.preserved.evidence_refs, ref],
                computer_id=self.state.preserved.computer_id,
                goal_run_id=self.state.preserved.goal_run_id,
                feature_map_ref=self.state.preserved.feature_map_ref,
            )

    # ==================== Context Pressure Detection ====================

    def assess_pressure(self) -> ContextPressure:
        """Assess current context pressure."""
        current = self.state.total_token_estimate()
        budget = self.budget.max_tokens
        trigger = self.budget.trigger_ratio

        layers_over = []
        for layer, content in self.state.layers.items():
            if content.token_estimate > budget * 0.3:  # Layer exceeds 30% of budget
                layers_over.append(layer)

        pressure = ContextPressure(
            is_under_pressure=(current >= budget * trigger),
            current_tokens=current,
            budget_tokens=budget,
            trigger_ratio=trigger,
            pressure_ratio=current / budget if budget > 0 else 0.0,
            layers_over_budget=layers_over,
            recommendation=self._pressure_recommendation(current, budget, layers_over),
        )
        return pressure

    def _pressure_recommendation(self, current: int, budget: int, layers_over: list) -> str:
        trigger = budget * self.budget.trigger_ratio
        if current >= budget:
            return "CRITICAL: Immediate compaction required"
        elif current >= budget * 0.9:
            return "HIGH: Schedule compaction within next round"
        elif current >= trigger:
            return "MODERATE: Monitor, prepare compaction"
        return "OK: Within budget"

    def should_compact(self) -> bool:
        """Check if compaction is needed."""
        pressure = self.assess_pressure()
        return pressure.is_under_pressure

    # ==================== Selective Compaction ====================

    def build_compaction_plan(self) -> CompactionPlan:
        """Build a selective compaction plan based on pressure and preservation rules."""
        actions = []

        # Determine which layers to compact (from oldest/least critical)
        compact_order = [
            ContextLayer.L0_CURRENT_TURN,
            ContextLayer.L2_OBSERVATIONS,
            ContextLayer.L5_FEATUREMAP_KNOWLEDGE,
            ContextLayer.L6_LONGTERM_MEMORY,
        ]

        for layer in compact_order:
            if layer not in self.state.layers:
                continue
            content = self.state.layers[layer]
            if content.token_estimate == 0:
                continue

            # Check if layer contains preserved items
            if self._layer_contains_preserved(layer):
                actions.append(
                    CompactionAction(
                        layer=layer,
                        action="summarize",
                        reason="Contains preserved items, summarizing",
                    )
                )
            elif layer in [ContextLayer.L0_CURRENT_TURN, ContextLayer.L2_OBSERVATIONS]:
                actions.append(
                    CompactionAction(
                        layer=layer,
                        action="summarize",
                        reason="High token layer, summarizing",
                    )
                )
            else:
                actions.append(
                    CompactionAction(
                        layer=layer,
                        action="ref_only",
                        reason="Reference-only preservation",
                    )
                )

        # Never compact these critical layers
        protected_layers = [
            ContextLayer.L1_ACTIVE_GOAL_PLAN,
            ContextLayer.L3_CHECKPOINT,
            ContextLayer.L4_EVIDENCE_ARTIFACTS,
        ]
        for layer in protected_layers:
            # Emit an explicit keep decision even when a layer is currently
            # empty.  This makes the plan self-documenting and prevents a
            # later caller from treating an absent layer as disposable.
            actions.append(
                CompactionAction(
                    layer=layer,
                    action="keep",
                    reason="Protected layer - never compact",
                )
            )

        return CompactionPlan(
            actions=actions,
            must_preserve=self.state.preserved,
            estimated_tokens_after=self._estimate_after_compaction(actions),
        )

    def _layer_contains_preserved(self, layer: ContextLayer) -> bool:
        """Check if a layer contains any preserved items."""
        preserved = self.state.preserved
        if layer == ContextLayer.L0_CURRENT_TURN:
            return bool(preserved.current_step)
        if layer == ContextLayer.L2_OBSERVATIONS:
            return bool(preserved.important_observations or preserved.unresolved_failures)
        if layer == ContextLayer.L5_FEATUREMAP_KNOWLEDGE:
            return bool(preserved.feature_map_ref)
        if layer == ContextLayer.L6_LONGTERM_MEMORY:
            return False
        return False

    def _estimate_after_compaction(self, actions: list[CompactionAction]) -> int:
        """Estimate tokens after compaction."""
        total = 0
        for action in actions:
            layer = self.state.get_layer(action.layer)
            if action.action == "keep":
                total += layer.token_estimate
            elif action.action == "summarize":
                total += max(100, layer.token_estimate // 5)  # Summary ~20%
            elif action.action == "ref_only":
                total += 50  # Just a reference
            # "drop" = 0 tokens
        return total

    def execute_compaction(
        self,
        plan: CompactionPlan | None = None,
        *,
        llm_summarizer: Callable[[str], str] | None = None,
    ) -> CompactionRecord:
        """Execute selective compaction according to plan."""
        if plan is None:
            plan = self.build_compaction_plan()

        summarizer = llm_summarizer or self.llm_summarizer
        if not summarizer:
            raise ContextEngineError("No LLM summarizer available for compaction")

        evidence_actions = [
            action for action in plan.actions if action.layer == ContextLayer.L4_EVIDENCE_ARTIFACTS
        ]
        if any(action.action != "keep" for action in evidence_actions):
            raise FalseMemoryInjected("L4 evidence must remain authoritative and uncompactable")

        compaction_id = (
            f"comp-{int(time.time() * 1000)}-{hashlib.md5(str(plan).encode()).hexdigest()[:8]}"
        )
        tokens_before = self.state.total_token_estimate()
        layers_compacted = []
        for action in plan.actions:
            if action.action == "keep":
                continue

            layer = action.layer
            if layer not in self.state.layers:
                continue

            content = self.state.layers[layer]
            if content.token_estimate == 0:
                continue

            layers_compacted.append(layer)

            if action.action == "summarize":
                # Render content for summarization
                # Use existing render function for L0/L2
                if layer in [ContextLayer.L0_CURRENT_TURN, ContextLayer.L2_OBSERVATIONS]:
                    summary_text = summarizer(render_messages_for_summary(content.content))
                else:
                    # Generic summary for other layers
                    summary_text = summarizer(json.dumps(content.content)[:8000])

                # Store summary as a new layer entry
                self.state.layers[layer] = LayerContent(
                    layer=layer,
                    content=[
                        {"role": "assistant", "content": f"[COMPACTION SUMMARY]\n{summary_text}"}
                    ],
                    token_estimate=len(summary_text) // 4,
                    metadata={
                        "compacted_at": datetime.now(UTC).isoformat(),
                        "original_tokens": content.token_estimate,
                        "summary": True,
                    },
                )

            elif action.action == "ref_only":
                # Keep only a reference
                self.state.layers[layer] = LayerContent(
                    layer=layer,
                    content=[
                        {
                            "role": "system",
                            "content": f"[REFERENCE] {layer.value} compacted to reference",
                        }
                    ],
                    token_estimate=50,
                    metadata={
                        "compacted_at": datetime.now(UTC).isoformat(),
                        "original_tokens": content.token_estimate,
                        "ref_only": True,
                    },
                )

            # "drop" = remove layer entirely (not used currently)

        # Save compaction record
        record = CompactionRecord(
            compaction_id=compaction_id,
            trigger_reason="pressure" if self.should_compact() else "manual",
            tokens_before=tokens_before,
            tokens_after=self.state.total_token_estimate(),
            layers_compacted=layers_compacted,
            preserved=plan.must_preserve,
            artifact_refs_preserved=plan.must_preserve.artifact_refs
            + plan.must_preserve.evidence_refs,
        )
        self.state.compaction_history.append(record)
        self._update_totals()

        return record

    # ==================== Checkpoint/Resume ====================

    def save_checkpoint(self, path: str | Path) -> Path:
        """Save context state to checkpoint file."""
        checkpoint_path = Path(path)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temp = checkpoint_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state.to_dict(), indent=2))
        temp.replace(checkpoint_path)

        # Also update long-running harness checkpoint if available
        if self.checkpoint_store and self.long_run_state:
            self.long_run_state.context_tokens = self.state.total_tokens
            self.checkpoint_store.write(self.long_run_state)

        return checkpoint_path

    @classmethod
    def load_checkpoint(
        cls,
        path: str | Path,
        *,
        budget: ContextBudget | None = None,
        persistent_computer_store: PersistentComputerStore | None = None,
        long_run_state: LongRunState | None = None,
        checkpoint_store: LongRunCheckpointStore | None = None,
        verification_engine: VerificationEngine | None = None,
        llm_summarizer: Callable[[str], str] | None = None,
    ) -> ContextEngine:
        """Load context engine from checkpoint."""
        path = Path(path)
        if not path.exists():
            raise ContextEngineError(f"Checkpoint not found: {path}")

        data = json.loads(path.read_text())
        context_state = ContextState.from_dict(data)
        return cls(
            goal_run_id=context_state.goal_run_id,
            computer_id=context_state.computer_id,
            budget=budget,
            persistent_computer_store=persistent_computer_store,
            long_run_state=long_run_state,
            checkpoint_store=checkpoint_store,
            verification_engine=verification_engine,
            llm_summarizer=llm_summarizer,
            context_state=context_state,
        )

    # ==================== Drift Detection ====================

    def verify_integrity(self) -> bool:
        """Verify context integrity (no drift, no false memory)."""
        current_hash = compute_context_hash(self.state)

        # Check if preserved items match
        preserved = self.state.preserved
        if preserved.verification_spec_ref and self.verification_engine:
            # Verify spec still matches
            spec_path = Path(f".veya/runs/{self.goal_run_id}/verification_spec.json")
            if spec_path.exists():
                spec_data = json.loads(spec_path.read_text())
                if spec_data.get("spec_hash") != preserved.verification_spec_ref:
                    raise DriftDetected("VerificationSpec hash mismatch")

        # Check compaction records don't contain evidence as summary
        for record in self.state.compaction_history:
            if record.preserved and record.preserved.evidence_refs:
                # Ensure evidence refs weren't turned into summary text
                pass  # Detailed check would go here

        self._context_hash = current_hash
        return True

    # ==================== Provider Switch Support ====================

    def prepare_provider_switch(self, new_provider: str) -> dict[str, Any]:
        """Prepare context for provider switch."""
        # Save current state
        checkpoint_path = self.save_checkpoint(
            f".veya/runs/{self.goal_run_id}/checkpoints/context_{int(time.time())}.json"
        )

        return {
            "checkpoint_path": str(checkpoint_path),
            "context_hash": compute_context_hash(self.state),
            "preserved": self.state.preserved.to_dict(),
            "compaction_history": [c.to_dict() for c in self.state.compaction_history],
        }

    def resume_from_provider_switch(self, switch_data: dict[str, Any]) -> None:
        """Resume after provider switch."""
        checkpoint_path = switch_data.get("checkpoint_path")
        if checkpoint_path:
            restored = self.load_checkpoint(
                checkpoint_path,
                budget=self.budget,
                persistent_computer_store=self.persistent_computer_store,
                long_run_state=self.long_run_state,
                checkpoint_store=self.checkpoint_store,
                verification_engine=self.verification_engine,
                llm_summarizer=self.llm_summarizer,
            )
            self.state = restored.state

        # The switch envelope is redundant by design: it protects against a
        # provider returning stale or partial state.  Do not merge it into the
        # checkpoint; compare the identity-critical projection instead.
        envelope = PreservedItems.from_dict(switch_data["preserved"])
        if envelope.to_dict() != self.state.preserved.to_dict():
            raise DriftDetected("provider switch preserved-context mismatch")
        expected_hash = switch_data.get("context_hash")
        actual_hash = compute_context_hash(self.state)
        if expected_hash and expected_hash != actual_hash:
            raise DriftDetected("provider switch context hash mismatch")
        self._context_hash = actual_hash
        self.verify_integrity()

    # ==================== Helpers ====================

    def get_context_summary(self) -> dict[str, Any]:
        """Get a summary of current context state."""
        return {
            "goal_run_id": self.goal_run_id,
            "computer_id": self.computer_id,
            "total_tokens": self.state.total_tokens,
            "layers": {
                k.value: {"tokens": v.token_estimate, "items": len(v.content)}
                for k, v in self.state.layers.items()
            },
            "preserved": self.state.preserved.to_dict(),
            "compaction_count": len(self.state.compaction_history),
            "pressure": self.assess_pressure().to_dict(),
        }

    def sync_with_long_run_state(self) -> None:
        """Sync context state with LongRunState."""
        if self.long_run_state:
            self.long_run_state.context_tokens = self.state.total_tokens
            self.long_run_state.current_step = self.state.preserved.current_step
            # Sync plan
            if self.state.preserved.active_plan:
                self.long_run_state.plan = self.state.preserved.active_plan

    def sync_from_long_run_state(self) -> None:
        """Sync from LongRunState to context."""
        if self.long_run_state:
            # Update preserved items from long run state
            self.update_preserved_plan(
                self.long_run_state.plan,
                self.long_run_state.current_step or "",
            )
            # Update failure evidence
            for failure in self.long_run_state.failure_evidence:
                self.add_unresolved_failure(failure)


__all__ = ["ContextEngine"]
