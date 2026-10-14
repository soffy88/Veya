# 3O-IO-ALLOW — tests/test_w16_routes.py: test W16 Project Brain REST routes
"""Unit and integration tests for W16 Project Brain REST routes (Spec §6 & §10).

Covers:
  - Legacy fallback when VEYA_PROJECT_BRAIN is OFF.
  - Project creation, directory layout, brief initialization, and listing when ON.
  - 404-not-403 owner privacy enforcement on cross-owner and missing projects.
  - Brief reading and updating (sections and raw markdown) via REST.
  - Tasks listing and task actions (cancel/pause/resume).
  - Messages proxy for project chat.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.project_brain.tasks import ProjectTask, upsert_task
from server.routes.projects import router as projects_router


@pytest.fixture
def test_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bot_root = tmp_path / "bots"
    registry_path = tmp_path / "bot_registry.json"
    projects_root = tmp_path / "projects"
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(bot_root))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(registry_path))
    monkeypatch.setenv("VEYA_PROJECTS_ROOT", str(projects_root))

    current_user_holder = {"user": {"user_id": "alice", "username": "alice"}}

    def get_current_user_override():
        return current_user_holder["user"]

    def require_user_override():
        u = current_user_holder["user"]
        if u.get("user_id") == "anonymous":
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="not authenticated")
        return u

    app = FastAPI()
    app.dependency_overrides[auth_mod.get_current_user] = get_current_user_override
    app.dependency_overrides[auth_mod.require_user] = require_user_override
    app.include_router(projects_router)

    client = TestClient(app)
    return client, current_user_holder, tmp_path


def test_legacy_fallback_when_flag_off(test_app, monkeypatch: pytest.MonkeyPatch):
    client, _, _ = test_app
    monkeypatch.setenv("VEYA_PROJECT_BRAIN", "0")

    # Legacy list
    res = client.get("/projects")
    assert res.status_code == 200
    assert "projects" in res.json()

    res_api = client.get("/api/v1/projects")
    assert res_api.status_code == 200

    # Legacy create
    created = client.post("/projects", json={"name": "Legacy Demo"})
    assert created.status_code == 200
    pid = created.json()["project"]["id"]

    # Extended endpoints return 404 when flag is off
    assert client.get(f"/api/v1/projects/{pid}/brief").status_code == 404
    assert client.get(f"/api/v1/projects/{pid}/tasks").status_code == 404
    assert client.get(f"/api/v1/projects/{pid}/messages").status_code == 404
    assert client.post(f"/api/v1/projects/{pid}/tasks/t1/cancel").status_code == 404


def test_create_and_list_projects_flag_enabled(test_app, monkeypatch: pytest.MonkeyPatch):
    client, user_holder, tmp_path = test_app
    monkeypatch.setenv("VEYA_PROJECT_BRAIN", "1")
    user_holder["user"] = {"user_id": "alice", "username": "alice"}

    custom_root = tmp_path / "custom_workspaces" / "project_alpha"
    created = client.post(
        "/api/v1/projects",
        json={
            "name": "Alpha Project",
            "description": "Main core project",
            "workspace_path": str(custom_root),
        },
    )
    assert created.status_code == 200
    pdata = created.json()["project"]
    pid = pdata["id"]
    bot_id = pdata["bot_id"]
    assert bot_id == f"proj-{pid}"
    assert pdata["owner_id"] == "alice"
    assert pdata["workspace_path"] == str(custom_root.resolve())

    # Verify .veya-project layout on disk
    store_dir = custom_root / ".veya-project"
    assert (store_dir / "PROJECT_STATE.md").exists()
    assert (store_dir / "brief.json").exists()
    assert (store_dir / "log").exists()

    # List projects - visible to alice
    listed = client.get("/api/v1/projects")
    assert listed.status_code == 200
    projects = listed.json()["projects"]
    assert any(p["id"] == pid and p["name"] == "Alpha Project" for p in projects)

    # Both /projects and /api/v1/projects work
    listed_legacy_path = client.get("/projects")
    assert listed_legacy_path.status_code == 200
    assert any(p["id"] == pid for p in listed_legacy_path.json()["projects"])


def test_owner_isolation_404_not_403(test_app, monkeypatch: pytest.MonkeyPatch):
    client, user_holder, _tmp_path = test_app
    monkeypatch.setenv("VEYA_PROJECT_BRAIN", "1")

    # Alice creates a project
    user_holder["user"] = {"user_id": "alice", "username": "alice"}
    created = client.post("/api/v1/projects", json={"name": "Alice Secret"})
    pid = created.json()["project"]["id"]

    # Bob logs in
    user_holder["user"] = {"user_id": "bob", "username": "bob"}

    # Bob's project list does NOT contain Alice's project
    bobs_list = client.get("/api/v1/projects").json()["projects"]
    assert not any(p["id"] == pid for p in bobs_list)

    # Bob attempting to access Alice's project gets 404 (not 403)
    assert client.get(f"/api/v1/projects/{pid}").status_code == 404
    assert client.get(f"/api/v1/projects/{pid}/brief").status_code == 404
    assert (
        client.put(
            f"/api/v1/projects/{pid}/brief", json={"section": "goal", "content": "hack"}
        ).status_code
        == 404
    )
    assert client.get(f"/api/v1/projects/{pid}/messages").status_code == 404
    assert client.post(f"/api/v1/projects/{pid}/messages", json={"text": "hack"}).status_code == 404
    assert client.get(f"/api/v1/projects/{pid}/tasks").status_code == 404
    assert client.post(f"/api/v1/projects/{pid}/tasks/t1/cancel").status_code == 404

    # Non-existent project also gets 404
    assert client.get("/api/v1/projects/non-existent-proj").status_code == 404


def test_brief_read_and_update(test_app, monkeypatch: pytest.MonkeyPatch):
    client, user_holder, _tmp_path = test_app
    monkeypatch.setenv("VEYA_PROJECT_BRAIN", "1")
    user_holder["user"] = {"user_id": "alice", "username": "alice"}

    created = client.post("/api/v1/projects", json={"name": "Beta System"})
    pid = created.json()["project"]["id"]

    # Read brief
    brief_res = client.get(f"/api/v1/projects/{pid}/brief")
    assert brief_res.status_code == 200
    data = brief_res.json()
    assert "brief" in data
    assert "markdown" in data

    # Update section: goal
    put_res = client.put(
        f"/api/v1/projects/{pid}/brief",
        json={"section": "goal_scope", "content": "Build high-speed trading system"},
    )
    assert put_res.status_code == 200
    assert put_res.json()["brief"]["goal_scope"] == "Build high-speed trading system"

    # Update section: rules
    put_rules = client.put(
        f"/api/v1/projects/{pid}/brief",
        json={"rules": ["- when: latency > 5ms -> do: scale workers [approval: auto]"]},
    )
    assert put_rules.status_code == 200
    rules = put_rules.json()["brief"]["rules"]
    assert len(rules) == 1
    assert rules[0]["when"] == "latency > 5ms"
    assert rules[0]["approval"] == "auto"

    # Update raw markdown
    raw_md = (
        "# Project State\n"
        "## 1. 目标与范围\nUltra Fast Service\n\n"
        "## 2. 重要文件\n- src/main.rs\n\n"
        "## 3. 规则\n- when: memory > 80% -> do: restart [approval: ask]\n\n"
        "## 4. 进行中任务与阻塞\n暂无\n\n"
        "## 5. 约定\nRust only\n"
    )
    put_md = client.put(
        f"/api/v1/projects/{pid}/brief",
        json={"markdown": raw_md},
    )
    assert put_md.status_code == 200
    assert put_md.json()["brief"]["goal_scope"] == "Ultra Fast Service"
    assert put_md.json()["brief"]["key_files"] == ["src/main.rs"]


def test_tasks_list_and_actions(test_app, monkeypatch: pytest.MonkeyPatch):
    client, user_holder, tmp_path = test_app
    monkeypatch.setenv("VEYA_PROJECT_BRAIN", "1")
    user_holder["user"] = {"user_id": "alice", "username": "alice"}

    proj_dir = tmp_path / "task_proj"
    created = client.post(
        "/api/v1/projects",
        json={"name": "Task Proj", "workspace_path": str(proj_dir)},
    )
    pid = created.json()["project"]["id"]

    # Seed a task into tasks.json
    task = ProjectTask(
        task_id="task-99",
        objective="Run comprehensive tests",
        status="running",
        verifier={"usable": True, "evidence": "pytest passed"},
        fix_notes=["note 1"],
    )
    upsert_task(proj_dir, task)

    # Get tasks list
    res = client.get(f"/api/v1/projects/{pid}/tasks")
    assert res.status_code == 200
    tasks = res.json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == "task-99"
    assert tasks[0]["verifier"]["usable"] is True

    # Pause action
    res_pause = client.post(f"/api/v1/projects/{pid}/tasks/task-99/pause")
    assert res_pause.status_code == 200
    assert res_pause.json()["action"] == "pause"

    # Resume action
    res_resume = client.post(f"/api/v1/projects/{pid}/tasks/task-99/resume")
    assert res_resume.status_code == 200
    assert res_resume.json()["action"] == "resume"
    assert res_resume.json()["status"] == "running"

    # Cancel action
    res_cancel = client.post(f"/api/v1/projects/{pid}/tasks/task-99/cancel")
    assert res_cancel.status_code == 200
    assert res_cancel.json()["action"] == "cancel"
    assert res_cancel.json()["status"] == "cancelled"

    # Invalid action
    res_bad = client.post(f"/api/v1/projects/{pid}/tasks/task-99/destroy")
    assert res_bad.status_code == 400

    # Non-existent task action
    res_missing = client.post(f"/api/v1/projects/{pid}/tasks/non-existent/cancel")
    assert res_missing.status_code == 404


def test_project_messages_proxy(test_app, monkeypatch: pytest.MonkeyPatch):
    client, user_holder, _tmp_path = test_app
    monkeypatch.setenv("VEYA_PROJECT_BRAIN", "1")
    user_holder["user"] = {"user_id": "alice", "username": "alice"}

    created = client.post("/api/v1/projects", json={"name": "Chat Proj"})
    pid = created.json()["project"]["id"]

    # Read messages
    res = client.get(f"/api/v1/projects/{pid}/messages")
    assert res.status_code == 200
    assert res.json()["bot_id"] == f"proj-{pid}"
    assert isinstance(res.json()["messages"], list)

    # Post message with async_mode mocked or handled
    from unittest.mock import AsyncMock, patch

    from server.bot_team.async_turns import AsyncTurnRef

    mock_ref = AsyncTurnRef(
        task_id="task-async-1",
        session_id="s-1",
        bot_id=f"proj-{pid}",
        kind="bot_turn",
    )
    with patch("server.routes.bot_team.start_bot_turn_async", new=AsyncMock(return_value=mock_ref)):
        post_res = client.post(
            f"/api/v1/projects/{pid}/messages?async=1",
            json={"text": "Start working on feature X"},
        )
        assert post_res.status_code == 202
        assert post_res.json()["task_id"] == "task-async-1"
