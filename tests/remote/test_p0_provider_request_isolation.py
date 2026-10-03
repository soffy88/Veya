import pytest

from veya.remote.execution import ExecutionPhase, ExecutionRecord
from veya.remote.provider_request import ProviderRequest, ProviderRequestStatus


def _record() -> ExecutionRecord:
    return ExecutionRecord(
        execution_id="ex-1", task_id="task-1", session_id="s", token_id="t",
        principal="p", tool="worker.dispatch", veya_tool="worker.dispatch",
        requested_workspace="/tmp/ws", requested_realpath="/tmp/ws",
        resolved_repo_root="/tmp/ws", repo_identity="repo",
    )


def test_provider_request_has_independent_identity_and_lifecycle():
    req = ProviderRequest(execution_id="ex-1", goal_run_id="goal-1", provider="opencode", model="space-bunny")
    assert req.request_id != req.execution_id
    assert req.status is ProviderRequestStatus.REQUESTED
    req.transition(ProviderRequestStatus.RUNNING, now=100.0)
    req.timeout(now=110.0)
    assert req.status is ProviderRequestStatus.TIMED_OUT
    assert req.elapsed_ms == 10_000


def test_provider_timeout_does_not_make_execution_terminal():
    record = _record()
    record.phase = str(ExecutionPhase.RUNNING)
    record.provider_request = ProviderRequest(execution_id=record.execution_id, goal_run_id="goal-1")
    record.provider_request.transition(ProviderRequestStatus.RUNNING, now=100.0)
    record.provider_request.timeout(now=110.0)
    assert record.provider_request.status is ProviderRequestStatus.TIMED_OUT
    assert record.is_terminal is False
    assert record.phase == str(ExecutionPhase.RUNNING)


def test_provider_request_terminal_state_is_idempotent_only_for_same_state():
    req = ProviderRequest(execution_id="ex-1")
    req.transition(ProviderRequestStatus.RUNNING, now=1.0)
    req.timeout(now=2.0)
    req.timeout(now=2.0)
    with pytest.raises(ValueError):
        req.transition(ProviderRequestStatus.COMPLETED, now=3.0)


def test_provider_request_survives_execution_store_round_trip(tmp_path):
    from veya.remote.execution import ExecutionStore
    record = _record()
    record.provider_request = ProviderRequest(execution_id=record.execution_id, goal_run_id="goal-1", provider="opencode")
    record.provider_request.transition(ProviderRequestStatus.RUNNING, now=10.0)
    record.provider_request.timeout(now=20.0)
    store = ExecutionStore(tmp_path)
    store.save(record)
    loaded = store.get(record.execution_id)
    assert loaded is not None
    assert loaded.provider_request is not None
    assert loaded.provider_request.request_id == record.provider_request.request_id
    assert loaded.provider_request.status is ProviderRequestStatus.TIMED_OUT
