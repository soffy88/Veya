"""tests/test_fixb_skill_eval_gate.py — FIXB tests for Real Paired Skill Evaluation Gate.

Tests:
1. evaluate_skill_candidate runs a real paired pipeline (Candidate vs Parent/baseline)
   over source trajectory + similar task set (eval dataset).
2. Scores are computed from verifiable metrics (task_completion, tool_success, output_consistency),
   NOT hardcoded constants.
3. Candidate with higher/equal verifiable score passes gate; regression / critical error blocks gate.
4. Scores and evaluation samples (prompts, outputs, metrics, deltas) are written into eval_evidence.
5. Deterministic runner test verifies full pipeline execution and metric calculation.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.skill_eval_gate import (
    build_evaluation_dataset,
    compute_verifiable_metrics,
    evaluate_skill_candidate,
)

# ---------------------------------------------------------------------------
# Test 1: Verifiable metrics computation (task_completion, tool_success, output_consistency)
# ---------------------------------------------------------------------------


def test_compute_verifiable_metrics():
    """Verify metrics calculation is non-constant and follows verifiable metrics contract."""
    # Case A: Perfect run (completed, tools succeeded, output matches expected)
    metrics_a = compute_verifiable_metrics(
        completed=True,
        tool_calls=[{"tool": "calculate", "args": {"expr": "100 * 1.1"}}],
        tool_errors=[],
        output="The total price including tax is 110.",
        expected=110,
    )
    assert metrics_a["task_completion"] == 1.0
    assert metrics_a["tool_success"] == 1.0
    assert metrics_a["output_consistency"] == 1.0
    assert metrics_a["score"] == 1.0

    # Case B: Tool error during execution
    metrics_b = compute_verifiable_metrics(
        completed=True,
        tool_calls=[{"tool": "calc"}],
        tool_errors=[{"tool": "calc", "error": "division by zero"}],
        output="Could not compute total due to calc error.",
        expected=110,
    )
    assert metrics_b["task_completion"] == 1.0
    assert metrics_b["tool_success"] == 0.0
    assert metrics_b["output_consistency"] == 0.0
    assert metrics_b["score"] < 0.5

    # Case C: Output inconsistency
    metrics_c = compute_verifiable_metrics(
        completed=True,
        tool_calls=[],
        tool_errors=[],
        output="The calculated total is 999.",
        expected=110,
    )
    assert metrics_c["output_consistency"] == 0.0
    assert 0.3 <= metrics_c["score"] <= 0.5


# ---------------------------------------------------------------------------
# Test 2: Dataset builder combines source trajectory + similar/domain tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_evaluation_dataset():
    """Builds evaluation dataset containing source task + similar tasks."""
    trajectory = {
        "objective": "Calculate shipping discount for VIP customers",
        "expected": "20% discount",
    }
    dataset = await build_evaluation_dataset(
        trajectory=trajectory,
        candidate_name="vip_shipping_discount",
        user_id="user_test",
    )
    assert len(dataset) >= 2
    # First task is from source trajectory
    assert "shipping discount" in dataset[0]["input"].lower()
    # Second task is similar / complementary
    assert len(dataset[1]["input"]) > 0
    for task in dataset:
        assert "task_id" in task
        assert "input" in task
        assert "budget" in task


# ---------------------------------------------------------------------------
# Test 3: Real paired evaluation pipeline execution (deterministic runner)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paired_evaluation_pipeline_with_runner():
    """Runs candidate vs baseline through evaluation pipeline, computing real scores and samples."""
    candidate = {
        "name": "currency_converter",
        "description": "Convert USD to EUR at fixed rate 0.92: total = usd * 0.92",
    }
    trajectory = {
        "objective": "Convert 100 USD to EUR",
        "expected": "92",
    }

    async def mock_eval_runner(
        *, task: dict[str, Any], skill: dict[str, Any] | None, budget: int
    ) -> dict[str, Any]:
        """Deterministic mock runner simulating task execution with and without skill."""
        has_skill = skill is not None and "0.92" in skill.get("description", "")
        task_input = task.get("input", "")
        if has_skill:
            # Candidate produces accurate conversion
            output = "100 USD is 92 EUR" if "100" in task_input else "Converted amount: 92"
            return {
                "completed": True,
                "tool_calls": [{"tool": "fx_rate"}],
                "tool_errors": [],
                "output": output,
                "critical_error": False,
            }
        else:
            # Baseline without skill does not know conversion rate
            return {
                "completed": True,
                "tool_calls": [],
                "tool_errors": [],
                "output": "I am not sure of the current exchange rate.",
                "critical_error": False,
            }

    result = await evaluate_skill_candidate(
        candidate=candidate,
        trajectory=trajectory,
        user_id="user_test",
        eval_runner=mock_eval_runner,
    )

    assert result["passed"] is True
    assert result["candidate_score"] > result["parent_score"]
    assert result["delta"] > 0.0
    assert result["candidate_wins"] >= 1
    assert result["critical_errors"] == 0

    # Verify samples contain verifiable details
    samples = result.get("samples", [])
    assert len(samples) >= 1
    sample0 = samples[0]
    assert sample0["candidate"]["score"] > sample0["parent"]["score"]
    assert "92" in sample0["candidate"]["output"]
    assert "task_completion" in sample0["candidate"]["metrics"]
    assert "tool_success" in sample0["candidate"]["metrics"]
    assert "output_consistency" in sample0["candidate"]["metrics"]


# ---------------------------------------------------------------------------
# Test 4: Regression / critical error blocks gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paired_evaluation_regression_blocks():
    """When candidate performs worse than baseline or has critical error -> gate blocks."""
    candidate = {
        "name": "broken_transfer",
        "description": "Transfer funds",
    }
    trajectory = {
        "objective": "Transfer 50 USD",
        "expected": "success",
    }

    async def mock_failing_runner(
        *, task: dict[str, Any], skill: dict[str, Any] | None, budget: int
    ) -> dict[str, Any]:
        if skill is not None:
            # Candidate crashes with critical error
            return {
                "completed": False,
                "tool_calls": [{"tool": "transfer"}],
                "tool_errors": [{"tool": "transfer", "error": "Fatal unauthorized error"}],
                "output": "Crash",
                "critical_error": True,
            }
        else:
            # Baseline completes safely
            return {
                "completed": True,
                "tool_calls": [],
                "tool_errors": [],
                "output": "Safety check passed: transfer blocked safely",
                "critical_error": False,
            }

    result = await evaluate_skill_candidate(
        candidate=candidate,
        trajectory=trajectory,
        user_id="user_test",
        eval_runner=mock_failing_runner,
    )

    assert result["passed"] is False
    assert result["critical_errors"] > 0 or result["delta"] < 0
