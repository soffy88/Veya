"""T1/T2/T3 + 事件溯源验收（SPEC §11 测试验收）。"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# T1: POST goal + todos → GET 投影字段与进度正确
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t1_goal_create_and_project(client):
    r = await client.post("/v1/loop/goals", json={
        "objective": "构建登录页",
        "todos": [
            {"id": "t1", "title": "设计 UI"},
            {"id": "t2", "title": "实现 API", "depends_on": ["t1"]},
        ],
    })
    assert r.status_code == 201
    goal = r.json()
    assert goal["objective"] == "构建登录页"
    assert goal["status"] == "active"
    assert set(goal["todos"]) == {"t1", "t2"}
    assert goal["todos"]["t1"]["status"] == "open"
    assert goal["todos"]["t2"]["depends_on"] == ["t1"]
    assert "render_text" in goal and "构建登录页" in goal["render_text"]
    goal_id = goal["goal_id"]

    r2 = await client.get(f"/v1/loop/goals/{goal_id}")
    assert r2.status_code == 200
    assert r2.json()["objective"] == "构建登录页"

    r3 = await client.get("/v1/loop/goals")
    assert r3.status_code == 200
    assert any(g["goal_id"] == goal_id for g in r3.json()["goals"])


# ---------------------------------------------------------------------------
# T2: update_todo done + evidence → 事件追加，投影含证据
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_update_todo_appends_events(client, store):
    r = await client.post("/v1/loop/goals", json={
        "objective": "发布 v1",
        "todos": [{"id": "t1", "title": "写测试"}],
    })
    goal_id = r.json()["goal_id"]

    r2 = await client.post(f"/v1/loop/goals/{goal_id}/todos/t1",
                           json={"status": "done", "evidence": "全部 42 个用例通过"})
    assert r2.status_code == 200
    goal = r2.json()
    assert goal["todos"]["t1"]["status"] == "done"
    assert goal["todos"]["t1"]["evidence"] == ["全部 42 个用例通过"]
    assert goal["status"] == "completed"  # 全部 done → GoalCompleted

    events = store.stream(aggregate_type="Goal", aggregate_id=goal_id)
    types = [e["event_type"] for e in events]
    assert types == ["GoalCreated", "TodoUpdated", "EvidenceAppended", "GoalCompleted"]


# ---------------------------------------------------------------------------
# T3: claim 未过期再 claim → 拒绝（fail-closed）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t3_claim_conflict(client):
    r = await client.post("/v1/loop/goals", json={
        "objective": "并行任务",
        "todos": [{"id": "t1", "title": "独占 todo"}],
    })
    goal_id = r.json()["goal_id"]

    r1 = await client.post(f"/v1/loop/goals/{goal_id}/todos/t1/claim", json={"lease_min": 45})
    assert r1.status_code == 200
    assert r1.json()["todos"]["t1"]["claim"]["claimant"] == "assistant"

    r2 = await client.post(f"/v1/loop/goals/{goal_id}/todos/t1/claim", json={"lease_min": 45})
    assert r2.status_code == 409  # 未过期再 claim → 拒绝
    assert "lease 未过期" in r2.json()["detail"]


# ---------------------------------------------------------------------------
# quota / gate / terminal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_gate_terminal(client):
    r = await client.post("/v1/loop/goals", json={
        "objective": "限额任务",
        "todos": [{"id": "t1", "title": "第一步"}],
    })
    goal_id = r.json()["goal_id"]

    sr = await client.post(f"/v1/loop/goals/{goal_id}/quota/should_run")
    assert sr.json()["should_run"] is True

    # spend 超预算 → 409
    sp = await client.post(f"/v1/loop/goals/{goal_id}/quota/spend",
                           json={"todo_id": "t1", "slots": 99})
    assert sp.status_code == 409

    # gate
    g = await client.post(f"/v1/loop/goals/{goal_id}/gates/check", json={"gate_scope": "design"})
    assert g.status_code == 200
    assert g.json()["resolved"] is False

    # terminal → 需审批，不自动执行
    t = await client.post(f"/v1/loop/goals/{goal_id}/terminal_check", json={"action": "delete"})
    assert t.json()["recommendation"] == "needs_approval"
    assert t.json()["auto_execute"] is False


# ---------------------------------------------------------------------------
# T8: 事件存储 append-only 与投影幂等
# ---------------------------------------------------------------------------


def test_event_store_append_only_and_replay(data_dir):
    from app.infra.event_store import EventStore

    store = EventStore(data_dir, tenant_id="default")
    store.append(aggregate_type="Goal", aggregate_id="g1", event_type="GoalCreated",
                 payload={"objective": "x", "todos": []})
    store.append(aggregate_type="Goal", aggregate_id="g1", event_type="TodoUpdated",
                 payload={"todo_id": "t1", "status": "in_progress"})

    # 重新加载 → 重放一致
    store2 = EventStore(data_dir, tenant_id="default")
    events = store2.stream(aggregate_type="Goal", aggregate_id="g1")
    assert [e["event_type"] for e in events] == ["GoalCreated", "TodoUpdated"]

    # 非法事件类型拒绝
    with pytest.raises(ValueError):
        store2.append(aggregate_type="Goal", aggregate_id="g1", event_type="Bogus", payload={})
