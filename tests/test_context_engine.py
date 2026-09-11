"""P1-D Context Engine: Failing tests first (TDD)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from runtime.context import (
    ContextLayer,
    ContextBudget,
    LayerContent,
    PreservedItems,
    CompactionRecord,
    ContextState,
    CompactionAction,
    CompactionPlan,
    ContextPressure,
    ContextEngineError,
    DriftDetected,
    compute_context_hash,
    ContextEngine,
)


class TestContextModels:
    """Test the core context models."""

    def test_context_layers_enum(self):
        """All 7 layers (L0-L6) exist."""
        assert len(ContextLayer) == 7
        assert ContextLayer.L0_CURRENT_TURN.value == "L0_current_turn"
        assert ContextLayer.L1_ACTIVE_GOAL_PLAN.value == "L1_active_goal_plan"
        assert ContextLayer.L2_OBSERVATIONS.value == "L2_observations"
        assert ContextLayer.L3_CHECKPOINT.value == "L3_checkpoint"
        assert ContextLayer.L4_EVIDENCE_ARTIFACTS.value == "L4_evidence_artifacts"
        assert ContextLayer.L5_FEATUREMAP_KNOWLEDGE.value == "L5_featuremap_knowledge"
        assert ContextLayer.L6_LONGTERM_MEMORY.value == "L6_longterm_memory"

    def test_layer_content(self):
        """LayerContent holds layer, content, token_estimate, metadata."""
        content = LayerContent(
            layer=ContextLayer.L0_CURRENT_TURN,
            content=[{"role": "user", "content": "hello"}],
            token_estimate=10,
            metadata={"test": "value"},
        )
        assert content.layer == ContextLayer.L0_CURRENT_TURN
        assert len(content.content) == 1
        assert content.token_estimate == 10

    def test_preserved_items(self):
        """PreservedItems holds all required preservation fields."""
        preserved = PreservedItems(
            objective="Test objective",
            verification_spec_ref="vspec-123",
            active_plan=["step1", "step2"],
            current_step="step1",
            unresolved_failures=[{"tool": "test", "error": "failed"}],
            important_observations=[{"obs": "key finding"}],
            artifact_refs=["artifact-1", "artifact-2"],
            evidence_refs=["evidence-1"],
            computer_id="comp-abc",
            goal_run_id="goal-xyz",
            feature_map_ref="fmap-123",
        )
        assert preserved.objective == "Test objective"
        assert preserved.verification_spec_ref == "vspec-123"
        assert len(preserved.active_plan) == 2
        assert preserved.current_step == "step1"
        assert len(preserved.unresolved_failures) == 1
        assert len(preserved.important_observations) == 1
        assert len(preserved.artifact_refs) == 2
        assert len(preserved.evidence_refs) == 1
        assert preserved.computer_id == "comp-abc"
        assert preserved.goal_run_id == "goal-xyz"
        assert preserved.feature_map_ref == "fmap-123"

    def test_compaction_record(self):
        """CompactionRecord tracks compaction events."""
        preserved = PreservedItems(artifact_refs=["art-1"], evidence_refs=["ev-1"])
        record = CompactionRecord(
            compaction_id="comp-123",
            trigger_reason="pressure",
            tokens_before=500000,
            tokens_after=250000,
            layers_compacted=[ContextLayer.L0_CURRENT_TURN, ContextLayer.L2_OBSERVATIONS],
            preserved=preserved,
            summary_ref="summary-ref-123",
        )
        assert record.compaction_id == "comp-123"
        assert record.tokens_before == 500000
        assert record.tokens_after == 250000
        assert len(record.layers_compacted) == 2
        assert record.preserved.artifact_refs == ["art-1"]

    def test_context_state_serialization(self):
        """ContextState serializes to/from dict correctly."""
        state = ContextState(
            goal_run_id="goal-1",
            computer_id="comp-1",
            layers={
                ContextLayer.L0_CURRENT_TURN: LayerContent(
                    layer=ContextLayer.L0_CURRENT_TURN,
                    content=[{"role": "user", "content": "test"}],
                    token_estimate=10,
                ),
            },
            preserved=PreservedItems(
                objective="Test",
                computer_id="comp-1",
                goal_run_id="goal-1",
            ),
        )
        data = state.to_dict()
        assert data["goal_run_id"] == "goal-1"
        assert data["computer_id"] == "comp-1"
        assert "L0_current_turn" in data["layers"]

        restored = ContextState.from_dict(data)
        assert restored.goal_run_id == "goal-1"
        assert restored.computer_id == "comp-1"
        assert ContextLayer.L0_CURRENT_TURN in restored.layers

    def test_compaction_action_and_plan(self):
        """CompactionAction and CompactionPlan work correctly."""
        action = CompactionAction(
            layer=ContextLayer.L0_CURRENT_TURN,
            action="summarize",
            reason="High token usage",
        )
        assert action.layer == ContextLayer.L0_CURRENT_TURN
        assert action.action == "summarize"

        plan = CompactionPlan(
            actions=[action],
            must_preserve=PreservedItems(
                objective="Test",
                computer_id="comp-1",
                goal_run_id="goal-1",
            ),
            estimated_tokens_after=100000,
        )
        assert len(plan.actions) == 1
        assert plan.estimated_tokens_after == 100000

    def test_context_pressure(self):
        """ContextPressure assessment works."""
        pressure = ContextPressure(
            is_under_pressure=True,
            current_tokens=800000,
            budget_tokens=1000000,
            trigger_ratio=0.7,
            pressure_ratio=0.8,
            layers_over_budget=[ContextLayer.L0_CURRENT_TURN],
            recommendation="HIGH: Schedule compaction",
        )
        assert pressure.is_under_pressure
        assert pressure.pressure_ratio == 0.8
        assert len(pressure.layers_over_budget) == 1


class TestContextEngine:
    """Test ContextEngine functionality."""

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def budget(self):
        return ContextBudget(max_tokens=100000, trigger_ratio=0.7)

    def test_context_layering(self, temp_dir, budget):
        """CONTEXT_LAYERING: 7 layers exist and can be managed."""
        engine = ContextEngine(
            goal_run_id="goal-1",
            computer_id="comp-1",
            budget=budget,
        )

        # Test all layers exist
        for layer in ContextLayer:
            content = engine.get_layer(layer)
            assert content.layer == layer

        # Test setting layers
        engine.set_layer(ContextLayer.L0_CURRENT_TURN, [{"role": "user", "content": "hello"}], 50)
        assert engine.get_layer(ContextLayer.L0_CURRENT_TURN).token_estimate == 50

        engine.append_to_layer(ContextLayer.L2_OBSERVATIONS, [{"tool": "test", "result": "ok"}], 30)
        assert engine.get_layer(ContextLayer.L2_OBSERVATIONS).token_estimate == 30

    def test_preserved_items_management(self, temp_dir, budget):
        """Goal, VerificationSpec, plan, failures, artifacts, computer_id preserved."""
        engine = ContextEngine(
            goal_run_id="goal-1",
            computer_id="comp-1",
            budget=budget,
        )

        # Set preserved items
        engine.update_preserved_objective("Test objective")
        engine.update_preserved_verification_spec("vspec-123")
        engine.update_preserved_plan(["step1", "step2"], "step1")
        engine.add_unresolved_failure({"tool": "test", "error": "failed"})
        engine.add_important_observation({"obs": "key finding"})
        engine.add_artifact_ref("artifact-1")
        engine.add_evidence_ref("evidence-1")

        preserved = engine.state.preserved
        assert preserved.objective == "Test objective"
        assert preserved.verification_spec_ref == "vspec-123"
        assert preserved.active_plan == ["step1", "step2"]
        assert preserved.current_step == "step1"
        assert len(preserved.unresolved_failures) == 1
        assert len(preserved.important_observations) == 1
        assert preserved.artifact_refs == ["artifact-1"]
        assert preserved.evidence_refs == ["evidence-1"]
        assert preserved.computer_id == "comp-1"
        assert preserved.goal_run_id == "goal-1"

    def test_pressure_detection(self, temp_dir, budget):
        """PRESSURE_DETECTION: Detects when context exceeds trigger_ratio."""
        engine = ContextEngine(
            goal_run_id="goal-1",
            computer_id="comp-1",
            budget=budget,
        )

        # Below threshold
        engine.set_layer(ContextLayer.L0_CURRENT_TURN, [{"role": "user", "content": "x" * 1000}], 20000)
        pressure = engine.assess_pressure()
        assert not pressure.is_under_pressure

        # Above threshold (70% of 100000 = 70000)
        engine.set_layer(ContextLayer.L2_OBSERVATIONS, [{"data": "x" * 1000}] * 10, 60000)
        pressure = engine.assess_pressure()
        assert pressure.is_under_pressure
        assert pressure.pressure_ratio >= 0.7

    def test_selective_compaction_plan(self, temp_dir, budget):
        """SELECTIVE_COMPACTION: Builds plan that preserves critical items."""
        engine = ContextEngine(
            goal_run_id="goal-1",
            computer_id="comp-1",
            budget=budget,
        )

        # Fill layers
        engine.set_layer(ContextLayer.L0_CURRENT_TURN, [{"role": "user", "content": "x" * 1000}] * 5, 40000)
        engine.set_layer(ContextLayer.L2_OBSERVATIONS, [{"data": "x" * 1000}] * 10, 50000)
        engine.set_layer(ContextLayer.L5_FEATUREMAP_KNOWLEDGE, [{"knowledge": "x" * 1000}] * 5, 30000)

        # Set preserved items
        engine.update_preserved_objective("Test")
        engine.update_preserved_verification_spec("vspec-123")
        engine.update_preserved_plan(["step1"], "step1")
        engine.add_unresolved_failure({"tool": "test", "error": "err"})
        engine.add_artifact_ref("artifact-1")
        engine.add_evidence_ref("evidence-1")

        # Build plan
        plan = engine.build_compaction_plan()

        # Verify critical layers are kept
        keep_actions = [a for a in plan.actions if a.action == "keep"]
        keep_layers = {a.layer for a in keep_actions}
        assert ContextLayer.L1_ACTIVE_GOAL_PLAN in keep_layers
        assert ContextLayer.L3_CHECKPOINT in keep_layers
        assert ContextLayer.L4_EVIDENCE_ARTIFACTS in keep_layers

        # Verify preserved items in plan
        assert plan.must_preserve.objective == "Test"
        assert plan.must_preserve.verification_spec_ref == "vspec-123"
        assert "artifact-1" in plan.must_preserve.artifact_refs
        assert "evidence-1" in plan.must_preserve.evidence_refs

    def test_goal_preservation(self, temp_dir, budget):
        """GOAL_PRESERVATION: Objective and plan survive compaction."""
        engine = ContextEngine(
            goal_run_id="goal-1",
            computer_id="comp-1",
            budget=budget,
        )
        engine.update_preserved_objective("Build a CLI tool")
        engine.update_preserved_plan(["design", "implement", "test"], "implement")

        # Simulate compaction
        engine.set_layer(ContextLayer.L0_CURRENT_TURN, [{"role": "user", "content": "x" * 1000}] * 10, 80000)
        plan = engine.build_compaction_plan()

        # Objective and plan in preserved
        assert engine.state.preserved.objective == "Build a CLI tool"
        assert engine.state.preserved.active_plan == ["design", "implement", "test"]
        assert engine.state.preserved.current_step == "implement"
        assert plan.must_preserve.objective == "Build a CLI tool"
        assert plan.must_preserve.active_plan == ["design", "implement", "test"]

    def test_verification_spec_preservation(self, temp_dir, budget):
        """VERIFICATION_SPEC_PRESERVATION: Spec hash/reference preserved."""
        engine = ContextEngine(
            goal_run_id="goal-1",
            computer_id="comp-1",
            budget=budget,
        )
        engine.update_preserved_verification_spec("vspec-abc123")

        # Simulate compaction
        engine.set_layer(ContextLayer.L0_CURRENT_TURN, [{"role": "user", "content": "x" * 1000}] * 10, 80000)
        plan = engine.build_compaction_plan()

        # Spec ref preserved
        assert engine.state.preserved.verification_spec_ref == "vspec-abc123"
        assert plan.must_preserve.verification_spec_ref == "vspec-abc123"

    def test_failure_context_preservation(self, temp_dir, budget):
        """FAILURE_CONTEXT_PRESERVATION: Unresolved failures preserved."""
        engine = ContextEngine(
            goal_run_id="goal-1",
            computer_id="comp-1",
            budget=budget,
        )
        engine.add_unresolved_failure({"tool": "cli", "error": "command not found", "step": "setup"})
        engine.add_important_observation({"phase": "setup", "finding": "missing dependency"})

        # Compaction should preserve these
        engine.set_layer(ContextLayer.L2_OBSERVATIONS, [{"data": "x" * 1000}] * 10, 80000)
        plan = engine.build_compaction_plan()

        assert len(engine.state.preserved.unresolved_failures) == 1
        assert len(engine.state.preserved.important_observations) == 1
        assert len(plan.must_preserve.unresolved_failures) == 1
        assert len(plan.must_preserve.important_observations) == 1

    def test_artifact_ref_preservation(self, temp_dir, budget):
        """ARTIFACT_REF_PRESERVATION: Artifact/evidence refs preserved."""
        engine = ContextEngine(
            goal_run_id="goal-1",
            computer_id="comp-1",
            budget=budget,
        )
        engine.add_artifact_ref("artifact-cli-test")
        engine.add_evidence_ref("evidence-verification-1")

        engine.set_layer(ContextLayer.L4_EVIDENCE_ARTIFACTS, [{"ref": "art-1"}] * 5, 30000)
        plan = engine.build_compaction_plan()

        # L4 is protected (keep action)
        keep_actions = [a for a in plan.actions if a.action == "keep"]
        assert ContextLayer.L4_EVIDENCE_ARTIFACTS in {a.layer for a in keep_actions}

        # Refs in preserved
        assert "artifact-cli-test" in engine.state.preserved.artifact_refs
        assert "evidence-verification-1" in engine.state.preserved.evidence_refs
        assert "artifact-cli-test" in plan.must_preserve.artifact_refs
        assert "evidence-verification-1" in plan.must_preserve.evidence_refs

    def test_checkpoint_resume(self, temp_dir, budget):
        """CHECKPOINT_RESUME: Save and restore context state."""
        engine = ContextEngine(
            goal_run_id="goal-1",
            computer_id="comp-1",
            budget=budget,
        )
        engine.update_preserved_objective("Test objective")
        engine.update_preserved_plan(["step1", "step2"], "step1")
        engine.add_artifact_ref("artifact-1")
        engine.set_layer(ContextLayer.L0_CURRENT_TURN, [{"role": "user", "content": "hello"}], 50)

        # Save checkpoint
        checkpoint_path = temp_dir / "context_checkpoint.json"
        engine.save_checkpoint(checkpoint_path)

        # Load from checkpoint
        engine2 = ContextEngine.load_checkpoint(
            checkpoint_path,
            budget=budget,
        )

        # Verify restored state
        assert engine2.goal_run_id == "goal-1"
        assert engine2.computer_id == "comp-1"
        assert engine2.state.preserved.objective == "Test objective"
        assert engine2.state.preserved.active_plan == ["step1", "step2"]
        assert engine2.state.preserved.current_step == "step1"
        assert "artifact-1" in engine2.state.preserved.artifact_refs
        assert engine2.get_layer(ContextLayer.L0_CURRENT_TURN).token_estimate == 50

    def test_provider_switch_resume(self, temp_dir, budget):
        """PROVIDER_SWITCH_RESUME: Context survives provider switch."""
        engine = ContextEngine(
            goal_run_id="goal-1",
            computer_id="comp-1",
            budget=budget,
        )
        engine.update_preserved_objective("Test")
        engine.update_preserved_verification_spec("vspec-123")
        engine.update_preserved_plan(["step1"], "step1")
        engine.add_unresolved_failure({"tool": "test", "error": "err"})
        engine.add_artifact_ref("artifact-1")
        engine.add_evidence_ref("evidence-1")

        # Prepare for provider switch
        switch_data = engine.prepare_provider_switch("provider-b")

        # Verify switch data contains all critical info
        assert "checkpoint_path" in switch_data
        assert "context_hash" in switch_data
        assert switch_data["preserved"]["objective"] == "Test"
        assert switch_data["preserved"]["verification_spec_ref"] == "vspec-123"
        assert switch_data["preserved"]["active_plan"] == ["step1"]
        assert len(switch_data["preserved"]["unresolved_failures"]) == 1
        assert "artifact-1" in switch_data["preserved"]["artifact_refs"]

        # Simulate provider switch + resume
        engine2 = ContextEngine(
            goal_run_id="goal-1",
            computer_id="comp-1",
            budget=budget,
        )
        engine2.resume_from_provider_switch(switch_data)

        # Verify resume
        assert engine2.state.preserved.objective == "Test"
        assert engine2.state.preserved.verification_spec_ref == "vspec-123"
        assert engine2.state.preserved.active_plan == ["step1"]
        assert "artifact-1" in engine2.state.preserved.artifact_refs
        assert "evidence-1" in engine2.state.preserved.evidence_refs

    def test_context_drift_zero(self, temp_dir, budget):
        """CONTEXT_DRIFT=0: No drift after compaction."""
        engine = ContextEngine(
            goal_run_id="goal-1",
            computer_id="comp-1",
            budget=budget,
        )
        engine.update_preserved_objective("Test")
        engine.update_preserved_verification_spec("vspec-123")
        engine.update_preserved_plan(["step1"], "step1")
        engine.add_artifact_ref("artifact-1")

        # Fill context to trigger compaction
        engine.set_layer(ContextLayer.L0_CURRENT_TURN, [{"role": "user", "content": "x" * 1000}] * 20, 90000)

        # Manual compaction
        plan = engine.build_compaction_plan()

        # Use simple summarizer for test
        def mock_summarizer(text: str) -> str:
            return "Summary of previous context"

        # Execute compaction
        record = engine.execute_compaction(plan, llm_summarizer=mock_summarizer)

        # Verify integrity after compaction
        engine.verify_integrity()

        # Verify preserved items intact
        assert engine.state.preserved.objective == "Test"
        assert engine.state.preserved.verification_spec_ref == "vspec-123"
        assert engine.state.preserved.active_plan == ["step1"]
        assert "artifact-1" in engine.state.preserved.artifact_refs

    def test_false_memory_zero(self, temp_dir, budget):
        """FALSE_MEMORY=0: No evidence turned into summary truth."""
        engine = ContextEngine(
            goal_run_id="goal-1",
            computer_id="comp-1",
            budget=budget,
        )
        engine.update_preserved_verification_spec("vspec-123")
        engine.add_evidence_ref("evidence-test-result")

        # Add evidence that should NOT be summarized
        engine.set_layer(ContextLayer.L4_EVIDENCE_ARTIFACTS, [
            {"type": "verification_result", "result": "PASS", "evidence_id": "evidence-test-result"}
        ], 1000)

        # Compaction should NOT summarize L4 (protected)
        plan = engine.build_compaction_plan()
        l4_actions = [a for a in plan.actions if a.layer == ContextLayer.L4_EVIDENCE_ARTIFACTS]
        assert all(a.action == "keep" for a in l4_actions)

        # L4 should not be in compacted layers
        engine.set_layer(ContextLayer.L0_CURRENT_TURN, [{"role": "user", "content": "x" * 1000}] * 20, 90000)
        plan = engine.build_compaction_plan()
        compacted_layers = [a.layer for a in plan.actions if a.action in ("summarize", "ref_only")]
        assert ContextLayer.L4_EVIDENCE_ARTIFACTS not in compacted_layers


import tempfile
import time
import json
import hashlib
from datetime import UTC, datetime

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
