from pathlib import Path

import pytest

from runtime.execution.checkpoint import (
    CheckpointError,
    DurableExecutionCheckpoint,
    ExecutionCheckpointStore,
)


def make_checkpoint(**overrides):
    values = dict(
        checkpoint_id="cp-1",
        goal_run_id="goal-1",
        execution_id="exec-1",
        provider_request_state={"request_id": "req-1", "status": "RUNNING"},
        worker_state={"pid": 123, "state": "PAUSED"},
        working_directory="/data/soffy/projects/veya",
        git_head="abc123",
        uncommitted_changes_digest="digest",
        last_event_sequence=42,
        last_completed_tool="pytest",
        resume_context={"next_action": "verify"},
        created_at="2026-10-03T00:00:00+00:00",
    )
    values.update(overrides)
    return DurableExecutionCheckpoint(**values)


def test_checkpoint_persists_all_resume_fields_and_hash(tmp_path: Path):
    store = ExecutionCheckpointStore(tmp_path)
    checkpoint = make_checkpoint()
    path = store.write_durable(checkpoint)
    restored = store.read_durable("cp-1")
    assert path.exists()
    assert restored is not None
    assert restored.to_dict() == checkpoint.with_content_hash().to_dict()
    assert restored.content_hash


def test_checkpoint_write_is_idempotent_for_same_id_and_content(tmp_path: Path):
    store = ExecutionCheckpointStore(tmp_path)
    checkpoint = make_checkpoint()
    first = store.write_durable(checkpoint)
    second = store.write_durable(checkpoint)
    assert first == second
    assert store.read_durable().checkpoint_id == "cp-1"


def test_checkpoint_id_conflict_is_rejected(tmp_path: Path):
    store = ExecutionCheckpointStore(tmp_path)
    store.write_durable(make_checkpoint())
    with pytest.raises(CheckpointError):
        store.write_durable(make_checkpoint(last_event_sequence=43))


def test_corrupt_or_tampered_checkpoint_is_rejected(tmp_path: Path):
    store = ExecutionCheckpointStore(tmp_path)
    store.write_durable(make_checkpoint())
    path = tmp_path / "checkpoints" / "cp-1.json"
    data = path.read_text(encoding="utf-8").replace('"last_event_sequence": 42', '"last_event_sequence": 99')
    path.write_text(data, encoding="utf-8")
    with pytest.raises(CheckpointError):
        store.read_durable("cp-1")
