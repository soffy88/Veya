from __future__ import annotations

import pytest

from server.tool_governance_adapter import TaskGovernanceContext


def test_legacy_result_distinguishes_not_attempted() -> None:
    with pytest.raises(RuntimeError, match=r"was not executed"):
        TaskGovernanceContext._legacy_result(
            "run_in_sandbox",
            {
                "status": "failed",
                "executed": False,
                "attempted": False,
                "error": {"type": "ActionNotAuthorized", "message": "denied"},
            },
        )


def test_legacy_result_preserves_attempted_failure() -> None:
    with pytest.raises(RuntimeError, match=r"execution failed \(ToolExecutionError\)") as caught:
        TaskGovernanceContext._legacy_result(
            "run_in_sandbox",
            {
                "status": "failed",
                "executed": False,
                "attempted": True,
                "failure_stage": "physical_execution",
                "physical_error_type": "ToolExecutionError",
                "physical_error_message": "exit_code=1; AssertionError",
            },
        )
    assert "AssertionError" in str(caught.value)
    assert "was not executed" not in str(caught.value)


def test_legacy_result_returns_completed_result() -> None:
    assert (
        TaskGovernanceContext._legacy_result(
            "run_in_sandbox",
            {"status": "completed", "executed": True, "result": {"exit_code": 0}},
        )
        == '{\n  "exit_code": 0\n}'
    )
