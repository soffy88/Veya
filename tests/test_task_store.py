"""server.task_store + P1-03 Task Center API 测试 (docs/VEYA_P1_P3_IMPLEMENTATION_SPEC.md §6)。

验证: Task 字段对齐规格 §6; 状态投影; workspace/status/session 过滤; 持久化;
cancel; A-04 约束 (本模块无任何"状态决定执行"的控制逻辑)。
"""

from __future__ import annotations

import pytest

from server.task_store import TaskProjection, TaskStore


def _store(tmp_path):
    return TaskStore(tmp_path / "tasks.json")


# ── TaskStore 基础 ───────────────────────────────────────────────────────


def test_create_and_get(tmp_path):
    store = _store(tmp_path)
    t = store.create(session_id="s1", title="写测试", objective="给 P1-03 写测试", workspace_id="ws1")

    assert t.id.startswith("task_")
    assert t.status == "pending"
    assert t.title == "写测试"
    assert t.workspace_id == "ws1"

    got = store.get(t.id)
    assert got is not None and got.objective == "给 P1-03 写测试"
    assert store.get("nonexistent") is None


def test_status_transitions_record_timestamps(tmp_path):
    store = _store(tmp_path)
    t = store.create(session_id="s1", title="t", objective="o")
    assert t.started_at is None and t.completed_at is None

    t = store.update_status(t.id, "running")
    assert t.status == "running" and t.started_at is not None

    t = store.update_status(t.id, "completed")
    assert t.status == "completed" and t.completed_at is not None

    t = store.update_status(t.id, "failed")
    assert t.status == "failed"

    # cancel 投影
    t2 = store.create(session_id="s2", title="t2", objective="o2")
    c = store.cancel(t2.id)
    assert c.status == "cancelled"


def test_list_filters_by_workspace_status_session(tmp_path):
    store = _store(tmp_path)
    a = store.create(session_id="s1", title="a", objective="oa", workspace_id="ws1")
    b = store.create(session_id="s1", title="b", objective="ob", workspace_id="ws2")
    c = store.create(session_id="s2", title="c", objective="oc", workspace_id="ws1")
    store.update_status(b.id, "running")

    assert {t.id for t in store.list(workspace_id="ws1")} == {a.id, c.id}
    assert {t.id for t in store.list(status="running")} == {b.id}
    assert {t.id for t in store.list(session_id="s2")} == {c.id}
    # 全量按 created_at 倒序
    assert store.list()[0].id == c.id


def test_persistence_across_instances(tmp_path):
    store = _store(tmp_path)
    t = store.create(session_id="s1", title="持久化", objective="o")
    store.update_status(t.id, "completed")

    reloaded = _store(tmp_path)
    got = reloaded.get(t.id)
    assert got.status == "completed"


def test_set_cost(tmp_path):
    store = _store(tmp_path)
    t = store.create(session_id="s1", title="t", objective="o")
    t = store.set_cost(t.id, 0.42)
    assert t.cost_usd == 0.42
    assert store.set_cost("nonexistent", 1.0) is None


def test_events_are_persisted_and_projection_rebuilds(tmp_path):
    store = _store(tmp_path)
    t = store.create(session_id="s1", title="事件", objective="回放")
    store.update_status(t.id, "running")
    store.set_cost(t.id, 0.42)
    store.update_status(t.id, "completed")

    topics = [event["topic"] for event in store.events(t.id)]
    assert topics == ["task.created", "task.started", "task.updated", "task.completed"]

    (tmp_path / "tasks.json").unlink()
    rebuilt = _store(tmp_path)
    got = rebuilt.get(t.id)
    assert got is not None
    assert got.status == "completed"
    assert got.cost_usd == 0.42


def test_projection_maps_approval_events(tmp_path):
    store = _store(tmp_path)
    t = store.create(session_id="s1", title="审批", objective="等待审批")
    approval = {
        "topic": "tool.approval_required",
        "task_id": t.id,
        "payload": {"task": t.to_dict()},
    }
    approved = {
        "topic": "tool.approved",
        "task_id": t.id,
        "payload": {"task": {**t.to_dict(), "status": "running"}},
    }

    projection = TaskProjection.from_events([approval, approved])
    assert projection.tasks[t.id]["status"] == "running"


def test_update_status_nonexistent_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.update_status("nonexistent", "completed") is None


def test_acceptance_and_checkpoint_are_projected(tmp_path):
    store = _store(tmp_path)
    task = store.create(
        session_id="s1",
        title="验收",
        objective="检查文件",
        acceptance=[{"id": "c1", "type": "file_exists", "path": "README.md"}],
    )
    assert task.acceptance[0]["id"] == "c1"
    updated = store.set_checkpoint(task.id, "checkpoint-1")
    assert updated.latest_checkpoint_id == "checkpoint-1"
    assert [event["topic"] for event in store.events(task.id)] == [
        "task.created",
        "checkpoint.created",
    ]


def test_acceptance_evidence_is_persisted(tmp_path):
    (tmp_path / "ok.txt").write_text("ok", encoding="utf-8")
    store = _store(tmp_path)
    task = store.create(
        session_id="s1",
        title="验收",
        objective="检查文件",
        acceptance=[{"id": "file", "type": "file_exists", "path": "ok.txt"}],
    )
    results = store.evaluate_acceptance(task.id, workspace=tmp_path)
    assert results and results[0]["status"] == "passed"
    assert store.get(task.id).acceptance[0]["evidence"]


# ── P1-03 API 路由 (server/routes/tasks.py) ──────────────────────────────


@pytest.mark.asyncio
async def test_tasks_api_roundtrip(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from server.routes.tasks import cancel_task, create_task, get_task, list_tasks, task_events
    from server.task_store import task_store

    # 重定向单例到临时路径, 不碰真实 ~/.veya
    monkeypatch.setattr(task_store, "path", tmp_path / "tasks.json")
    monkeypatch.setattr(task_store.event_store, "path", tmp_path / "events.jsonl")
    task_store._tasks = {}

    req = type("R", (), {"session_id": "s1", "title": "", "objective": "API 测试", "workspace_id": "ws1", "task_id": None})()
    r = await create_task(req)
    assert r["status"] == "created"
    tid = r["task"]["id"]

    r = await list_tasks(workspace="ws1")
    assert r["count"] == 1
    r = await list_tasks(status="pending")
    assert r["count"] == 1

    r = await get_task(tid)
    assert r["task"]["id"] == tid

    r = await cancel_task(tid)
    assert r["status"] == "cancelled"

    r = await task_events(tid)
    assert [event["topic"] for event in r["events"]] == [
        "task.created",
        "task.cancelled",
    ]

    with pytest.raises(HTTPException) as exc:
        await get_task("nonexistent")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_task_propagates_to_runtime(tmp_path, monkeypatch):
    from server.routes import tasks as tasks_routes
    from server.routes.tasks import cancel_task

    store = TaskStore(tmp_path / "tasks.json")
    task = store.create(session_id="sess-cancel", title="cancel", objective="stop me")
    monkeypatch.setattr(tasks_routes, "task_store", store)

    async def fake_cancel(session_id: str):
        assert session_id == "sess-cancel"
        return {"cancelled": ["chat_stream"]}

    import server.coordinator_master as coordinator_mod

    monkeypatch.setattr(coordinator_mod, "cancel_session", fake_cancel)
    result = await cancel_task(task.id)
    assert result["runtime"] == {"cancelled": ["chat_stream"]}
    assert result["task"]["status"] == "cancelled"
