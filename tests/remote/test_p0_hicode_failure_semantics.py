from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from runtime.execution.observability import ExecutionMetrics
from server.hicode_agent import _hicode_result_error
from veya.remote.execution import (
    DurableJobManager,
    ExecutionError,
    ExecutionStore,
    ExecutionType,
)


def _binding(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        requested_path=str(root),
        requested_realpath=str(root),
        repo_root=str(root),
        repo_identity=str(root),
        worktree_path=None,
        worktree_repo_root=None,
    )


def _session() -> SimpleNamespace:
    return SimpleNamespace(session_id="session-p0", token_id="token-p0", principal="test")


def test_empty_assistant_reply_is_typed_and_keeps_raw_evidence() -> None:
    result = {
        "type": "result",
        "result": "",
        "num_turns": 1,
        "tool_calls": [],
        "response": {
            "role": "assistant",
            "content": None,
            "tool_calls": [],
            "api_key": "must-not-leak",
        },
    }

    error = _hicode_result_error(result, raw_events=[], stderr_tail="provider-tail", exit_code=0)

    assert error is not None
    assert error.code == "EMPTY_MODEL_RESPONSE"
    assert error.raw_evidence["result"]["response"]["content"] is None
    assert error.raw_evidence["result"]["response"]["tool_calls"] == []
    assert error.raw_evidence["result"]["response"]["api_key"] == "<redacted>"
    assert error.raw_evidence["stderr_tail"] == "provider-tail"


async def test_root_error_survives_normal_activity_projection(tmp_path: Path) -> None:
    manager = DurableJobManager(ExecutionStore(tmp_path / "failure"))

    async def runner(reporter):
        reporter.failure(
            failure_class="EMPTY_MODEL_RESPONSE",
            source="hicode_provider",
            detail="provider returned an empty assistant response with no tool calls",
            code="EMPTY_MODEL_RESPONSE",
            raw_evidence={"response": {"content": None, "tool_calls": []}},
        )
        reporter.tool_activity(activity="bash 完成", count=False)
        raise ExecutionError(
            "EMPTY_MODEL_RESPONSE",
            "provider returned an empty assistant response with no tool calls",
        )

    record = manager.submit(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=runner,
        execution_type=str(ExecutionType.HICODE),
    )
    await manager.wait(record.execution_id, timeout_s=10)
    public = record.to_public(heartbeat_timeout_s=60.0)

    assert record.status == "FAILED"
    assert public["failure_class"] == "EMPTY_MODEL_RESPONSE"
    assert public["provider_error_code"] == "EMPTY_MODEL_RESPONSE"
    assert "empty assistant response" in public["failure_message"]
    assert public["failure_message"] != "bash 完成"
    assert public["raw_failure_evidence"]["response"]["content"] is None


async def test_first_error_wins_over_wrapper_failure(tmp_path: Path) -> None:
    manager = DurableJobManager(ExecutionStore(tmp_path / "first-error"))

    async def runner(reporter):
        reporter.failure(
            failure_class="EMPTY_MODEL_RESPONSE",
            source="hicode_provider",
            detail="first provider failure",
            code="EMPTY_MODEL_RESPONSE",
            raw_evidence={"first": True},
        )
        reporter.failure(
            failure_class="WORKER_EXECUTION_FAILED",
            source="hicode",
            detail="wrapper failure",
            code="WORKER_EXECUTION_FAILED",
            raw_evidence={"wrapper": True},
        )
        raise ExecutionError("WORKER_EXECUTION_FAILED", "wrapper failure")

    record = manager.submit(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=runner,
        execution_type=str(ExecutionType.HICODE),
    )
    await manager.wait(record.execution_id, timeout_s=10)
    public = record.to_public(heartbeat_timeout_s=60.0)

    assert public["failure_class"] == "EMPTY_MODEL_RESPONSE"
    assert public["failure_message"] == "first provider failure"
    assert [item["failure_class"] for item in public["failure_history"]][:2] == [
        "EMPTY_MODEL_RESPONSE",
        "WORKER_EXECUTION_FAILED",
    ]


async def test_recovered_round_does_not_pollute_success_projection(tmp_path: Path) -> None:
    manager = DurableJobManager(ExecutionStore(tmp_path / "recovered"))

    async def runner(reporter):
        reporter.failure(
            failure_class="ROUND_PROVIDER_FAILURE",
            source="hicode_provider",
            detail="round 0 failed",
            code="ROUND_PROVIDER_FAILURE",
            raw_evidence={"round": 0},
            round_index=0,
        )
        reporter.tool_activity(activity="recovery tool")
        return "recovered"

    record = manager.submit(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=runner,
        execution_type=str(ExecutionType.HICODE),
    )
    await manager.wait(record.execution_id, timeout_s=10)
    public = record.to_public(heartbeat_timeout_s=60.0)

    assert record.status == "COMPLETED"
    assert public["failure_class"] is None
    assert public["failure_message"] is None
    assert public["provider_error_code"] is None
    assert public["raw_failure_evidence"] is None
    assert public["failure_history"][0]["failure_class"] == "ROUND_PROVIDER_FAILURE"
    assert public["failure_history"][0]["recovered"] is True
    assert public["round_history"][0]["recovered"] is True


async def test_tool_calls_project_real_progress_and_unknown_total(tmp_path: Path) -> None:
    manager = DurableJobManager(ExecutionStore(tmp_path / "progress"))

    async def runner(reporter):
        for index in range(91):
            reporter.tool_activity(activity=f"tool-{index}")
        return "done"

    record = manager.submit(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=runner,
        execution_type=str(ExecutionType.HICODE),
    )
    await manager.wait(record.execution_id, timeout_s=10)
    public = record.to_public(heartbeat_timeout_s=60.0)

    assert public["tool_call_count"] == 91
    assert public["current_step"] == 91
    assert public["total_steps"] is None
    assert public["progress"] == {
        "unit": "tool_calls",
        "current": 91,
        "total": None,
        "tool_calls": 91,
        "model_requests": 0,
    }


def test_observability_keeps_structured_provider_failure_evidence() -> None:
    metrics = ExecutionMetrics()
    metrics.record_failure(
        provider=True,
        code="EMPTY_MODEL_RESPONSE",
        source="hicode_provider",
        detail="empty assistant response",
        evidence={"response": {"content": None, "tool_calls": []}},
    )

    snapshot = metrics.snapshot()
    assert snapshot["provider_failures"] == 1
    assert snapshot["failure_evidence"][-1]["code"] == "EMPTY_MODEL_RESPONSE"
    assert snapshot["failure_evidence"][-1]["evidence"]["response"]["content"] is None
