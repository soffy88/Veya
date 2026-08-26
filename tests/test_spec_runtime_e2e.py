"""Controlled Ask → Persist → Verify → Learn runtime proof for P1–P3."""

from __future__ import annotations

from server.capability_model import SkillRegistry, _JsonRegistryStore
from server.events import event_store
from server.memory_controller import MemoryController, _MemoryStore
from server.task_store import TaskStore
from server.trajectory import append_trajectory, build_trajectory, read_trajectories


def test_p1_p3_runtime_closure(tmp_path, monkeypatch):
    monkeypatch.setattr(event_store, "path", tmp_path / "events.jsonl")
    monkeypatch.setattr(event_store, "_known_event_ids", None)

    task_store = TaskStore(tmp_path / "tasks.json", event_path=tmp_path / "events.jsonl")
    task = task_store.create(
        session_id="sess_e2e",
        title="runtime proof",
        objective="persist and verify",
        trace_id="trace_e2e",
        acceptance=[{"id": "file", "type": "file_exists", "path": "answer.txt"}],
    )
    task_store.update_status(task.id, "running")
    (tmp_path / "answer.txt").write_text("done", encoding="utf-8")
    acceptance = task_store.evaluate_acceptance(task.id, workspace=tmp_path)
    task_store.set_checkpoint(task.id, "cp_e2e")
    task_store.update_status(task.id, "completed")

    memory = MemoryController(_MemoryStore(tmp_path / "memory.json"))
    record = memory.observe(
        "answer.txt is the verified output",
        scope_type="session",
        scope_id="sess_e2e",
        memory_type="episodic",
        source_event_ids=["task.created"],
        confidence=0.9,
    )
    corrected_id = memory.correct_record(
        record.memory_id,
        content="answer.txt is the verified final output",
        provenance="user_correction",
    )

    skills = SkillRegistry(_JsonRegistryStore(tmp_path / "skills.json"))
    skill = skills.propose_skill(
        "verify output files",
        {"trigger_examples": ["验收输出"], "source_event_ids": ["trajectory.recorded"]},
    )
    skills.confirm_skill(skill.skill_id)
    skills.record_usage(skill.skill_id, success=True, evidence=["file_exists:answer.txt"])

    trajectory_path = tmp_path / "trajectory.jsonl"
    append_trajectory(
        build_trajectory(
            task_id=task.id,
            objective=task.objective,
            outcome="completed",
            tool_calls=[{"tool": "file_exists", "ok": True}],
            duration_ms=12,
            acceptance_results=acceptance,
            recovery_actions=[{"action": "checkpoint_created", "checkpoint_id": "cp_e2e"}],
            trace_id="trace_e2e",
        ),
        path=trajectory_path,
    )

    topics = [event["topic"] for event in event_store.read_all()]
    assert "task.created" in topics
    assert "task.completed" in topics
    assert "checkpoint.created" in topics
    assert "memory.candidate_created" in topics
    assert "memory.corrected" in topics
    assert "skill.created" in topics
    assert "skill.executed" in topics
    assert "trajectory.recorded" in topics
    assert corrected_id and memory.get(record.memory_id).status == "deprecated"
    assert read_trajectories(task.id, path=trajectory_path)[0]["acceptance_results"][0]["status"] == "passed"
