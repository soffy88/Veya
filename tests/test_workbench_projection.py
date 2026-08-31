"""PR-14 Workbench projection and control-boundary contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server.events import EventStore
from server.task_store import TaskStore
from server.workbench_projection import WorkbenchProjection, redact_text


def _event(
    store: EventStore,
    task_id: str,
    session_id: str,
    topic: str,
    payload: dict[str, Any],
    *,
    trace_id: str = "trace-pr14",
) -> None:
    store.append(
        {
            "event_id": f"{topic}-{len(store.read_all())}",
            "task_id": task_id,
            "session_id": session_id,
            "trace_id": trace_id,
            "topic": topic,
            "actor": "test",
            "payload": payload,
        }
    )


@pytest.mark.asyncio
async def test_workbench_is_a_redacted_projection_of_existing_authorities(tmp_path: Path) -> None:
    events = EventStore(tmp_path / "events.jsonl")
    tasks = TaskStore(tmp_path / "tasks.json", event_store=events)
    task = tasks.create(
        session_id="session-pr14", title="Workbench", objective="inspect password=secret"
    )
    _event(events, task.id, task.session_id, "message.user_added", {"content": "inspect"})
    _event(events, task.id, task.session_id, "message.assistant_added", {"content": "done"})
    _event(
        events,
        "other-task",
        task.session_id,
        "message.assistant_added",
        {"content": "must not leak"},
        trace_id="other-trace",
    )
    _event(
        events,
        task.id,
        task.session_id,
        "tool.approval_required",
        {
            "request_id": "approval-1",
            "tool_name": "github_write",
            "tool_args": {"api_token": "secret"},
        },
    )
    _event(
        events,
        task.id,
        task.session_id,
        "action_gateway.audit",
        {"request_id": "req-1", "detail": {"decision": "REQUIRE_APPROVAL", "password": "secret"}},
    )
    _event(
        events,
        task.id,
        task.session_id,
        "browser.session_prepared",
        {
            "browser_handle": {
                "session_id": "browser-1",
                "computer_id": "computer-1",
                "control_state": "AGENT_CONTROL",
                "version": 3,
                "url": "https://example.test",
            }
        },
    )
    _event(
        events,
        task.id,
        task.session_id,
        "provider.usage",
        {"provider": "test", "model": "m", "usage": {"total_tokens": 4, "cost_usd": 0.01}},
    )

    view = await WorkbenchProjection(tasks=tasks, events=events, project_root=tmp_path).build(
        task.id
    )

    assert view is not None
    assert [message["role"] for message in view["conversation"]] == ["user", "assistant"]
    assert "must not leak" not in json.dumps(view, ensure_ascii=False)
    assert view["approvals"]["pending"][0]["request_id"] == "approval-1"
    assert view["browser"]["session_id"] == "browser-1"
    assert view["usage"]["records"][0]["total_tokens"] == 4
    encoded = json.dumps(view, ensure_ascii=False)
    assert "secret" not in encoded
    assert "[REDACTED]" in encoded


@pytest.mark.asyncio
async def test_approval_lifecycle_and_projection_version_are_deterministic(tmp_path: Path) -> None:
    events = EventStore(tmp_path / "events.jsonl")
    tasks = TaskStore(tmp_path / "tasks.json", event_store=events)
    task = tasks.create(session_id="session-pr14", title="Approval", objective="wait")
    _event(
        events,
        task.id,
        task.session_id,
        "tool.approval_required",
        {"request_id": "approval-2", "tool_name": "write_file"},
    )

    first = await WorkbenchProjection(tasks=tasks, events=events, project_root=tmp_path).build(
        task.id
    )
    assert first is not None
    assert len(first["approvals"]["pending"]) == 1
    _event(
        events,
        task.id,
        task.session_id,
        "tool.approved",
        {"request_id": "approval-2", "tool_name": "write_file"},
    )
    second = await WorkbenchProjection(tasks=tasks, events=events, project_root=tmp_path).build(
        task.id
    )
    assert second is not None
    assert second["approvals"]["pending"] == []
    assert first["state"]["version"] != second["state"]["version"]


@pytest.mark.asyncio
async def test_artifacts_are_allowlisted_and_secret_text_is_redacted(tmp_path: Path) -> None:
    events = EventStore(tmp_path / "events.jsonl")
    tasks = TaskStore(tmp_path / "tasks.json", event_store=events)
    task = tasks.create(session_id="session-pr14", title="Artifacts", objective="verify")
    output = tmp_path / ".veya" / "runs" / task.id / "outputs"
    output.mkdir(parents=True)
    (output / "verification_report.json").write_text(
        json.dumps({"status": "passed", "api_token": "secret"}), encoding="utf-8"
    )
    (output / "not-allowlisted.txt").write_text("secret", encoding="utf-8")

    view = await WorkbenchProjection(tasks=tasks, events=events, project_root=tmp_path).build(
        task.id
    )
    assert view is not None
    assert [item["name"] for item in view["artifacts"]] == ["verification_report.json"]
    assert redact_text("Authorization: Bearer abc123") == "Authorization: Bearer [REDACTED]"


@pytest.mark.asyncio
async def test_artifact_endpoint_redacts_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.routes import workbench as workbench_routes

    output = tmp_path / ".veya" / "runs" / "task-pr14" / "outputs"
    output.mkdir(parents=True)
    (output / "verification_report.json").write_text(
        json.dumps({"status": "passed", "authorization": "Bearer secret"}), encoding="utf-8"
    )

    class Projection:
        project_root = tmp_path

        async def build(self, _task_id: str) -> dict[str, Any]:
            return {"state": {"version": "v1"}}

    monkeypatch.setattr(workbench_routes, "projection", Projection())
    result = await workbench_routes.get_workbench_artifact("task-pr14", "verification_report.json")
    encoded = json.dumps(result, ensure_ascii=False)
    assert "secret" not in encoded
    assert "Bearer [REDACTED]" in encoded


@pytest.mark.asyncio
async def test_workbench_approval_rejects_stale_request_without_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from server.routes import workbench as workbench_routes
    from server.routes.workbench import ApprovalRequest

    class EmptyProjection:
        async def build(self, _task_id: str) -> dict[str, Any]:
            return {"state": {"version": "v1"}, "approvals": {"pending": []}}

    monkeypatch.setattr(workbench_routes, "projection", EmptyProjection())
    with pytest.raises(HTTPException) as raised:
        await workbench_routes.resolve_workbench_approval(
            "task-pr14", ApprovalRequest(request_id="expired", approved=True, expected_version="v1")
        )
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "STALE_APPROVAL"


@pytest.mark.asyncio
async def test_workbench_browser_control_delegates_to_existing_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.routes import workbench as workbench_routes
    from server.routes.workbench import BrowserControlRequest

    calls: list[str] = []

    class FakeAdapter:
        async def status(self, handle: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "handle": {**handle, "version": 4}}

        async def take_control(self, _handle: dict[str, Any]) -> dict[str, Any]:
            calls.append("take_control")
            return {
                "ok": True,
                "handle": {
                    "session_id": "browser-1",
                    "version": 5,
                    "control_state": "HUMAN_CONTROL",
                },
            }

        async def return_control(self, _handle: dict[str, Any]) -> dict[str, Any]:
            calls.append("return_control")
            return {
                "ok": True,
                "handle": {
                    "session_id": "browser-1",
                    "version": 6,
                    "control_state": "AGENT_CONTROL",
                },
            }

    view = {
        "state": {"version": "v1"},
        "browser": {"session_id": "browser-1", "version": 4, "control_state": "AGENT_CONTROL"},
        "approvals": {"pending": []},
    }

    class Projection:
        async def build(self, _task_id: str) -> dict[str, Any]:
            return view

    monkeypatch.setattr(workbench_routes, "projection", Projection())
    monkeypatch.setattr(workbench_routes, "get_browser_adapter", lambda _session_id: FakeAdapter())
    monkeypatch.setattr(workbench_routes, "append_canonical_event", lambda *args, **kwargs: None)

    result = await workbench_routes.control_workbench_browser(
        "task-pr14",
        BrowserControlRequest(action="takeover", expected_handle_version=4),
    )
    assert calls == ["take_control"]
    assert result["browser"]["session_id"] == "browser-1"
