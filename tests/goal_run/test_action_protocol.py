from __future__ import annotations

import pytest

from server.goal_run.action_protocol import CanonicalActionRequest, CanonicalActionResult


def test_canonical_action_round_trip_preserves_execution_context() -> None:
    request = CanonicalActionRequest(
        action_id="action-1",
        goal_run_id="goal-1",
        task_id="task-1",
        tool="run_in_sandbox",
        arguments={"command": "printf ok"},
        capability="computer",
        computer_ref="computer-1",
        context_ref="checkpoint-1",
        approval={"required": True},
        idempotency_key="goal-1:action-1",
        evidence_refs=("failure-0",),
    )
    restored = CanonicalActionRequest.from_dict(request.to_dict())
    assert restored == request
    assert restored.idempotency_key == "goal-1:action-1"

    result = CanonicalActionResult(
        action_id=request.action_id,
        status="failed",
        attempted=True,
        executed=False,
        result="physical failure",
        failure_evidence=(
            {"stage": "physical_execution", "error": "permission denied"},
        ),
        evidence_refs=("failure-0",),
    )
    assert CanonicalActionResult.from_dict(result.to_dict()) == result


def test_protocol_has_no_lifecycle_or_acceptance_authority() -> None:
    request_fields = set(CanonicalActionRequest.__dataclass_fields__)
    result_fields = set(CanonicalActionResult.__dataclass_fields__)
    assert not request_fields & {"goal_status", "retry", "verdict", "replan"}
    assert not result_fields & {"goal_status", "retry", "verdict", "replan"}


@pytest.mark.parametrize("missing", ["action_id", "goal_run_id", "task_id", "tool"])
def test_request_requires_authority_identity(missing: str) -> None:
    values = {
        "action_id": "a",
        "goal_run_id": "g",
        "task_id": "t",
        "tool": "tool",
    }
    values[missing] = ""
    with pytest.raises(ValueError):
        CanonicalActionRequest(**values)
