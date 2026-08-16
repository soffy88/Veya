"""G6: VS Code 扩展闭环测试(run-stream + SSE + run-agent + chat)。"""

import json
import os

import pytest

os.environ.setdefault("VEYA_SKIP_TEST_GATE", "1")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from server.app import app

    with TestClient(app) as c:
        yield c


def test_run_stream_returns_session_and_stream_url(client):
    r = client.post("/vscode/run-stream", json={"persona": "build", "text": "greet the user"})
    assert r.status_code == 200
    data = r.json()
    assert data["session_id"]
    assert data["stream_url"] == f"/stream/{data['session_id']}"
    assert data["status"] == "started"


def test_sse_flow_emits_lifecycle_events(client):
    """G6 闭环:发起任务 → SSE 流 → 生命周期事件序列。"""
    r = client.post("/vscode/run-stream", json={"persona": "build", "text": "greet the user"})
    sid = r.json()["session_id"]

    r = client.get(f"/stream/{sid}")
    assert r.status_code == 200
    raw = r.text
    assert "data: [DONE]" in raw

    events = [
        json.loads(line[6:])
        for line in raw.splitlines()
        if line.startswith("data:") and line != "data: [DONE]"
    ]
    types = [e.get("type") for e in events]
    assert types[0] == "session_start"
    assert "task_done" in types or "master_done" in types
    assert types[-1] in ("task_done", "master_done")


def test_run_agent_sync_route(client):
    """同步 run-agent 路由返回结构化结果(不崩溃)。"""
    r = client.post(
        "/vscode/run-agent",
        json={"agent": "build", "input_text": "greet the user"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["session_id"]
    assert data["status"] in ("completed", "error")
    assert isinstance(data["result"], (str, type(None)))


def test_chat_route(client):
    r = client.post("/vscode/chat", json={"message": "hi", "session_id": None})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] in ("completed", "success", "error")


def test_vscode_routes_do_not_use_dead_coordinator_api(client):
    """回归守卫:run-agent/chat 不再调用不存在的 coordinator.run/create_session。"""
    from server.routes import vscode as v

    with open(v.__file__, encoding="utf-8") as f:
        src = f.read()
    assert "coordinator.run(" not in src
    assert "from server.coordinator import" not in src
    assert "coordinator_master" in src
    assert "create_session(" not in src
