"""Trajectory → Eval → repeated pattern → gated learning candidate."""

from __future__ import annotations

from server.events import EventStore
from server.learning_engine import LearningEngine, _CandidateStore
from server.memory_controller import MemoryController, _MemoryStore


def test_repeated_evaluated_trajectories_create_only_a_candidate(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr("server.events.event_store", store)

    for task_id in ("task-a", "task-b"):
        store.append(
            {
                "topic": "trajectory.recorded",
                "trace_id": task_id,
                "session_id": task_id,
                "task_id": task_id,
                "payload": {
                    "task_id": task_id,
                    "objective": "verify the output",
                    "outcome": "completed",
                },
            }
        )
        store.append(
            {
                "topic": "eval.recorded",
                "trace_id": task_id,
                "session_id": task_id,
                "task_id": task_id,
                "payload": {"passed": True, "evaluator": "acceptance"},
            }
        )

    engine = LearningEngine(
        store=_CandidateStore(tmp_path / "candidates.json"),
        memory=MemoryController(_MemoryStore(tmp_path / "memory.json")),
    )
    findings = engine.reflect_trajectories()
    assert len(findings) == 1
    candidate = engine.propose(findings[0])
    assert candidate.status == "proposed"
    assert candidate.source_episode_ids == ["task-a", "task-b"]
    assert len(candidate.evidence_for) == 1
    assert engine._memory.get(candidate.evidence_for[0]).status == "candidate"
