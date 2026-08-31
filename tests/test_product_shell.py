"""Product Shell contract: default Bot identity, onboarding, and secret isolation."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.product_shell as product_shell
from server.routes.product import router


def test_default_bot_is_uninitialized_without_product_config(monkeypatch):
    monkeypatch.setattr(product_shell, "_load_config", lambda: {})

    state = product_shell.read_bot_state()

    assert state["bot"] == {
        "id": "veya-default",
        "name": "Veya Bot",
        "lifecycle": "uninitialized",
    }
    assert state["onboarding"]["required"] is True
    assert state["provider"]["credential"]["ref"] is None


def test_configured_state_never_returns_raw_credential(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fixture_marker = "fixture-marker"
    config = {
        "bot": {"onboarding_completed": True},
        "llm": {"provider": "openai", "model": "gpt-test"},
        "providers": {"openai": {"api_key": fixture_marker}},
        "workspace": str(workspace),
    }
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    state = product_shell.read_bot_state(config)

    assert state["bot"]["lifecycle"] == "ready"
    assert state["provider"]["configured"] is True
    assert fixture_marker not in json.dumps(state)


def test_existing_veya_init_config_is_product_ready(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    state = product_shell.read_bot_state(
        {
            "llm": {"provider": "ollama", "model": "local-test"},
            "workspace": str(workspace),
        }
    )

    assert state["onboarding"]["completed"] is True
    assert state["bot"]["lifecycle"] == "ready"


def test_onboarding_persists_reference_only(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    saved: dict = {}
    monkeypatch.setattr(product_shell, "_load_config", lambda: {})
    monkeypatch.setattr(product_shell, "_save_config", lambda config: saved.update(config))

    state = product_shell.configure_bot(
        provider="openai",
        model="gpt-test",
        workspace=str(workspace),
        credential_ref="local:web:openai",
    )

    assert state["bot"]["id"] == "veya-default"
    assert saved["providers"]["openai"] == {"credential_ref": "local:web:openai"}
    assert "api_key" not in json.dumps(saved)


def test_product_routes_expose_only_secret_free_state(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(product_shell, "_load_config", lambda: {})
    client = TestClient(app)

    response = client.get("/api/v1/bot")

    assert response.status_code == 200
    assert response.json()["bot"]["id"] == "veya-default"
    assert "api_key" not in response.text


def test_product_api_does_not_persist_or_echo_raw_credential(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    persisted: dict = {}
    monkeypatch.setattr(product_shell, "_load_config", lambda: {})
    monkeypatch.setattr(product_shell, "_save_config", lambda config: persisted.update(config))
    client = TestClient(app)

    response = client.post(
        "/api/v1/bot/onboarding",
        json={"provider": "ollama", "model": "fixture-model", "api_key": "fixture-marker"},
    )

    assert response.status_code == 200
    assert "fixture-marker" not in response.text
    assert "api_key" not in json.dumps(persisted)


def test_product_shell_fixture_dogfood(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    persisted: dict = {}
    monkeypatch.setattr(product_shell, "_load_config", lambda: dict(persisted))
    monkeypatch.setattr(product_shell, "_save_config", lambda config: persisted.update(config))

    assert product_shell.read_bot_state()["bot"]["lifecycle"] == "uninitialized"
    configured = product_shell.configure_bot(
        provider="ollama",
        model="fixture-model",
        workspace=str(workspace),
    )
    restored = product_shell.read_bot_state()

    assert configured["bot"]["id"] == "veya-default"
    assert configured["bot"]["lifecycle"] == "ready"
    assert restored["bot"] == configured["bot"]
    assert restored["recovery"]["tasks"] == "/api/v1/tasks"


@pytest.mark.asyncio
async def test_product_task_entry_creates_canonical_task_and_delegates_to_master(
    monkeypatch, tmp_path
):
    from server.events import EventStore
    from server.routes import product as product_routes
    from server.routes.product import ProductTaskRequest
    from server.task_store import TaskStore

    events = EventStore(tmp_path / "events.jsonl")
    tasks = TaskStore(tmp_path / "tasks.json", event_store=events)
    history_saves: list[tuple[str, dict[str, Any]]] = []
    delegated: list[dict[str, Any]] = []

    class History:
        async def save(self, session_id: str, messages: list[Any], *, user_id: str) -> None:
            history_saves.append((session_id, {"user_id": user_id, "messages": messages}))

    def append_local_event(
        topic: str,
        payload: dict[str, Any] | None = None,
        *,
        actor: str = "system",
        session_id: str | None = None,
        trace_id: str | None = None,
        turn_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        return events.append(
            {
                "topic": topic,
                "payload": payload or {},
                "actor": actor,
                "session_id": session_id,
                "trace_id": trace_id,
                "turn_id": turn_id,
                "task_id": task_id,
            }
        )

    async def fake_run(**kwargs: Any) -> None:
        delegated.append(kwargs)

    monkeypatch.setattr(product_routes, "task_store", tasks)
    monkeypatch.setattr(product_routes, "default_history_store", lambda: History())
    monkeypatch.setattr(product_routes, "append_canonical_event", append_local_event)
    monkeypatch.setattr(product_routes, "_run_product_task", fake_run)
    product_routes._product_tasks.clear()

    result = await product_routes.create_product_task(
        ProductTaskRequest(
            objective="在隔离工作区创建一个小的验证文件",
            config={"request_mode": "ephemeral"},
        ),
        {"user_id": "product-test"},
    )
    await asyncio.gather(*list(product_routes._product_tasks))

    task_id = result["task_id"]
    stored = tasks.get(task_id)
    assert result["status"] == "accepted"
    assert result["workbench_url"] == f"/workbench/{task_id}"
    assert stored is not None
    assert history_saves and history_saves[0][1]["user_id"] == "product-test"
    assert delegated and delegated[0]["task_id"] == task_id
    assert delegated[0]["session_id"] == result["session_id"]
    assert [event["topic"] for event in events.read_all(task_id=task_id)] == [
        "task.created",
        "product.task_submitted",
    ]
    encoded = json.dumps(
        {"result": result, "task": stored.to_dict(), "events": events.read_all()},
        ensure_ascii=False,
    )
    assert "request_mode" not in encoded


@pytest.mark.asyncio
async def test_product_task_runner_passes_existing_task_identity_to_master(monkeypatch):
    from server.coordinator_master import master_coordinator
    from server.routes import product as product_routes

    calls: list[dict[str, Any]] = []

    async def fake_chat_stream(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"args": args, "kwargs": kwargs})
        return {"final_answer": "ok", "error": None}

    monkeypatch.setattr(master_coordinator, "chat_stream", fake_chat_stream)
    await product_routes._run_product_task(
        task_id="task-product-1",
        session_id="session-product-1",
        objective="safe task",
        provider="ollama",
        model="fixture-model",
        config={},
        user={"user_id": "product-test"},
    )

    assert calls[0]["args"] == ("safe task",)
    assert calls[0]["kwargs"] == {
        "session_id": "session-product-1",
        "task_id": "task-product-1",
        "config": None,
        "provider": "ollama",
        "model": "fixture-model",
        "require_approval": True,
    }


def test_goal_run_events_keep_product_task_context(monkeypatch, tmp_path):
    from server import events as events_module
    from server.events import EventStore, _task_id_ctx, bind_event_context, reset_event_context
    from server.goal_run.store import append_event

    canonical = EventStore(tmp_path / "canonical-events.jsonl")
    monkeypatch.setattr(events_module, "event_store", canonical)
    context_tokens = bind_event_context(
        session_id="session-goal-1", trace_id="trace-goal-1", turn_id="turn-goal-1"
    )
    task_token = _task_id_ctx.set("task-goal-1")
    try:
        append_event(tmp_path, "goal-1", {"type": "goal_started"})
    finally:
        _task_id_ctx.reset(task_token)
        reset_event_context(context_tokens)

    projected = canonical.read_all()
    assert projected[0]["topic"] == "goal.started"
    assert projected[0]["session_id"] == "session-goal-1"
    assert projected[0]["task_id"] == "task-goal-1"
    assert projected[0]["trace_id"] == "trace-goal-1"


@pytest.mark.asyncio
async def test_resume_keeps_the_original_product_task_projection(monkeypatch, tmp_path):
    from server import events as events_module
    from server.coordinator_master import master_coordinator
    from server.events import EventStore
    from server.routes import tasks as task_routes
    from server.routes.tasks import TaskResumeRequest
    from server.task_store import TaskStore

    events = EventStore(tmp_path / "events.jsonl")
    tasks = TaskStore(tmp_path / "tasks.json", event_store=events)
    task = tasks.create(session_id="session-resume-1", title="Resume", objective="continue")
    calls: list[dict[str, Any]] = []

    async def fake_chat_stream(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"args": args, "kwargs": kwargs})
        return {"status": "completed", "error": None, "final_answer": "resumed"}

    monkeypatch.setattr(task_routes, "task_store", tasks)
    monkeypatch.setattr(events_module, "event_store", events)
    monkeypatch.setattr(master_coordinator, "chat_stream", fake_chat_stream)

    result = await task_routes.resume_task(
        task.id,
        TaskResumeRequest(text="continue the same task", max_rounds=2),
    )

    assert result["task_id"] == task.id
    assert calls[0]["kwargs"] == {
        "session_id": task.session_id,
        "task_id": task.id,
        "max_rounds": 2,
    }
    assert [item.id for item in tasks.list()] == [task.id]


@pytest.mark.asyncio
async def test_master_reuses_precreated_task_without_duplicate_projection(monkeypatch, tmp_path):
    from server import events as events_module
    from server.coordinator_master import MasterCoordinator
    from server.events import EventStore
    from server.task_store import TaskStore
    from veya.oservi.history_store import SqliteHistoryStore

    events = EventStore(tmp_path / "events.jsonl")
    tasks = TaskStore(tmp_path / "tasks.json", event_store=events)
    task = tasks.create(
        session_id="session-master-1",
        title="Master reuse",
        objective="answer",
        trace_id="trace-master-1",
    )

    async def text_llm(_messages: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:
        return {
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {},
        }

    import server.task_store as task_module

    monkeypatch.setattr(task_module, "task_store", tasks)
    monkeypatch.setattr(events_module, "event_store", events)
    monkeypatch.setenv("VEYA_MEMORY", "0")
    monkeypatch.setenv("VEYA_SESSION_TREE_MIRROR_ENABLED", "0")
    coordinator = MasterCoordinator(
        llm_fn=text_llm,
        max_rounds=1,
        history_store=SqliteHistoryStore(tmp_path / "history.db"),
    )

    result = await coordinator.chat_stream("answer", session_id=task.session_id, task_id=task.id)

    scoped = events.read_all(task_id=task.id)
    assert result["status"] == "success"
    assert sum(event["topic"] == "task.created" for event in scoped) == 1
    assert any(event["topic"] == "message.user_added" for event in scoped)
    assert any(event["topic"] == "message.assistant_added" for event in scoped)
    assert tasks.list(session_id=task.session_id)[0].id == task.id
