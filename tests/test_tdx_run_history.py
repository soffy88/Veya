"""TDX Run History only projects actual canonical events."""

from __future__ import annotations

from fastapi.testclient import TestClient

from server import events as events_mod
from server.app import app
from server.events import EventStore

client = TestClient(app)


def test_run_history_flag_off(monkeypatch, owner_headers):
    monkeypatch.delenv("VEYA_TDX_RUN_HISTORY", raising=False)
    assert client.get("/api/v1/tdx/runs", headers=owner_headers).status_code == 404


def test_run_history_empty_store_returns_no_samples(monkeypatch, tmp_path, owner_headers):
    monkeypatch.setenv("VEYA_TDX_RUN_HISTORY", "true")
    monkeypatch.setattr(events_mod, "event_store", EventStore(tmp_path / "events.jsonl"))

    response = client.get("/api/v1/tdx/runs", headers=owner_headers)
    assert response.status_code == 200
    assert response.json() == {"enabled": True, "runs": [], "count": 0}
    assert client.get("/api/v1/tdx/runs/nonexistent/rounds", headers=owner_headers).status_code == 404
    assert client.get("/api/v1/tdx/runs/nonexistent/compare?a=1&b=2", headers=owner_headers).status_code == 404


def test_run_history_and_round_comparison_come_from_canonical_events(monkeypatch, tmp_path, owner_headers):
    monkeypatch.setenv("VEYA_TDX_RUN_HISTORY", "true")
    store = EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr(events_mod, "event_store", store)

    store.append({
        "topic": "task.started", "task_id": "real-run-42", "session_id": "session-42",
        "actor": "test", "ts": 100.0,
        "payload": {"round": 1, "plan": "Inspect backend", "model": "model-real", "token_usage": {"total": 80, "prompt_tokens": 50, "completion_tokens": 30}},
    })
    store.append({
        "topic": "task.completed", "task_id": "real-run-42", "session_id": "session-42",
        "actor": "test", "ts": 110.0,
        "payload": {"round": 1, "status": "completed", "duration_s": 10, "commit_sha": "sha-round-1", "evidence_tags": ["tdx-ci"], "diff_summary": {"files_changed": 2, "additions": 20, "deletions": 4}, "passed": 3, "failed": 1},
    })
    store.append({
        "topic": "supervision.evidence_recorded", "task_id": "real-run-42", "session_id": "session-42",
        "actor": "supervisor", "ts": 111.0,
        "payload": {"round": 1, "evidence_chain": ["supervision-evidence-1"]},
    })
    store.append({
        "topic": "task.started", "task_id": "real-run-42", "session_id": "session-42",
        "actor": "test", "ts": 120.0,
        "payload": {"round": 2, "plan": "Fix tests", "model": "model-real", "token_usage": {"total": 100, "prompt_tokens": 65, "completion_tokens": 35}},
    })
    store.append({
        "topic": "task.completed", "task_id": "real-run-42", "session_id": "session-42",
        "actor": "test", "ts": 126.0,
        "payload": {"round": 2, "status": "completed", "duration_s": 6, "commit_sha": "sha-round-2", "evidence_tags": ["tdx-ci", "manual-review"], "diff_summary": {"files_changed": 1, "additions": 6, "deletions": 1}, "passed": 4, "failed": 0},
    })
    # Events without a durable task/run ID are deliberately not assigned a fake ID.
    store.append({"topic": "task.completed", "session_id": "only-session", "actor": "test", "ts": 130.0, "payload": {"status": "completed"}})

    response = client.get("/api/v1/tdx/runs", headers=owner_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    run = data["runs"][0]
    assert run["task_id"] == "real-run-42"
    assert run["round_count"] == 2
    assert run["plan"] == "Fix tests"
    assert run["duration_s"] == 6
    assert run["rounds"][0]["commit_sha"] == "sha-round-1"
    assert run["rounds"][1]["commit_sha"] == "sha-round-2"

    rounds_response = client.get("/api/v1/tdx/runs/real-run-42/rounds", headers=owner_headers)
    assert rounds_response.status_code == 200
    rounds = rounds_response.json()["rounds"]
    assert [row["round"] for row in rounds] == [1, 2]
    assert rounds[0]["test_counts"] == {"passed": 3, "failed": 1}
    assert rounds[0]["evidence"] == ["supervision-evidence-1"]
    assert rounds[0]["commit_sha"] == "sha-round-1"
    assert rounds[0]["evidence_tags"] == ["tdx-ci"]
    assert rounds[1]["commit_sha"] == "sha-round-2"

    comparison = client.get("/api/v1/tdx/runs/real-run-42/compare?a=1&b=2", headers=owner_headers)
    assert comparison.status_code == 200
    diff = comparison.json()["diff_comparison"]
    assert diff["duration_delta_s"] == -4
    assert diff["diff_delta"] == {"additions": -14, "deletions": -3, "files_changed": -1}
    assert diff["tests_delta"] == {"passed": 1, "failed": -1}
    assert diff["token_usage_delta"] == {"total": 20, "prompt_tokens": 15, "completion_tokens": 5}
    assert "commit_sha" in diff["changed_fields"]
    assert "evidence_tags" in diff["changed_fields"]


def test_run_history_infers_rounds_from_repeated_starts_when_no_round_metadata(monkeypatch, tmp_path):
    from server.routes.tdx import _project_runs

    projected = _project_runs([
        {"topic": "task.started", "task_id": "repeat-task", "ts": 1.0, "payload": {"plan": "first"}},
        {"topic": "task.completed", "task_id": "repeat-task", "ts": 2.0, "payload": {"status": "completed"}},
        {"topic": "task.started", "task_id": "repeat-task", "ts": 3.0, "payload": {"plan": "second"}},
        {"topic": "task.completed", "task_id": "repeat-task", "ts": 4.0, "payload": {"status": "failed"}},
    ])
    rounds = projected["repeat-task"]
    assert [item["round"] for item in rounds] == [1, 2]
    assert [item["plan"] for item in rounds] == ["first", "second"]
