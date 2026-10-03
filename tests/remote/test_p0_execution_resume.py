from types import SimpleNamespace

import pytest

from runtime.execution.checkpoint import DurableExecutionCheckpoint
from veya.remote.execution import DurableJobManager, ExecutionError, ExecutionStore


class Session(SimpleNamespace):
    session_id = "session-1"
    token_id = "token-1"
    principal = "principal-1"

async def runner(_reporter):
    return "resumed"

def checkpoint_for(execution_id, goal_run_id, workspace):
    return DurableExecutionCheckpoint(
        checkpoint_id="cp-resume-1", goal_run_id=goal_run_id, execution_id=execution_id,
        provider_request_state={"status": "TIMED_OUT"}, worker_state={"state": "PAUSED"},
        working_directory=workspace, git_head="head-1", uncommitted_changes_digest="dirty-1",
        last_event_sequence=7, last_completed_tool="pytest",
        resume_context={"next_action": "continue"}, created_at="2026-10-03T00:00:00+00:00",
    )

def make_manager(tmp_path):
    store = ExecutionStore(tmp_path / "executions")
    manager = DurableJobManager(store=store, recovery_runner_factory=lambda _r: runner)
    binding = SimpleNamespace(requested_path=str(tmp_path), requested_realpath=str(tmp_path),
        repo_root=str(tmp_path), repo_identity="git:test", worktree_path=str(tmp_path),
        worktree_repo_root=str(tmp_path), base_sha="head-1")
    original = manager.submit(session=Session(), tool="test", veya_tool="worker", binding=binding,
        runner=runner, goal_run_id="goal-1", goal_task_id="task-1", goal_project_root=str(tmp_path))
    original.status = "TIMED_OUT"
    original.phase = "TIMED_OUT"
    manager._persist(original, required=True)
    manager.checkpoint_store.write_durable(checkpoint_for(original.execution_id, "goal-1", str(tmp_path)))
    return manager, original

@pytest.mark.asyncio
async def test_resume_creates_new_execution_and_preserves_goal_run(tmp_path):
    manager, original = make_manager(tmp_path)
    resumed = await manager.resume(original.execution_id, token_id="token-1")
    assert resumed.execution_id != original.execution_id
    assert resumed.goal_run_id == "goal-1"
    assert resumed.resumed_from_execution_id == original.execution_id
    assert resumed.checkpoint_id == "cp-resume-1"

@pytest.mark.asyncio
async def test_resume_is_idempotent(tmp_path):
    manager, original = make_manager(tmp_path)
    first = await manager.resume(original.execution_id, token_id="token-1", idempotency_key="resume-1")
    second = await manager.resume(original.execution_id, token_id="token-1", idempotency_key="resume-1")
    assert second.execution_id == first.execution_id

@pytest.mark.asyncio
async def test_resume_rejects_checkpoint_lineage_mismatch(tmp_path):
    manager, original = make_manager(tmp_path)
    manager.checkpoint_store.write_durable(checkpoint_for("other-exec", "goal-1", str(tmp_path)).__class__(**{**checkpoint_for("other-exec", "goal-1", str(tmp_path)).to_dict(), "checkpoint_id": "cp-other"}))
    with pytest.raises(ExecutionError, match="checkpoint does not belong"):
        await manager.resume(original.execution_id, token_id="token-1")
