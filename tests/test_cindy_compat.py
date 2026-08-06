"""Cindy 兼容端点门禁 — 根 app (server/app.py) 的插件市场 / 定时任务。

背景: 线上 Caddy 把 /api/v1/* 反代到 Agent OS 主 app (根 server.app),
前端插件市场 (plugin/manage) 与定时任务 (scheduler) 因此必须挂载在根 app。
本测试直接对根 app 发请求, 防止该能力面回退。
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform" / "3O" / "oskill"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform" / "3O" / "obase"))

from server.app import app

client = TestClient(app)


def test_root_app_scheduler_list():
    """定时任务: 根 app 必须提供 /api/v1/scheduler (list)。"""
    res = client.post("/api/v1/scheduler", json={"action": "list"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert "schedules" in data
    assert isinstance(data["schedules"], list)


def test_root_app_plugin_marketplace():
    """插件市场: 根 app 必须提供 /api/v1/plugin/manage (marketplace)。"""
    res = client.post("/api/v1/plugin/manage", json={"action": "marketplace"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert "marketplace" in data
    assert "installed" in data


def test_root_app_plugin_unknown_action():
    """非法 action 被 Literal 枚举拒绝 (422, 与 veya L4 行为一致, 非 500)。"""
    res = client.post("/api/v1/plugin/manage", json={"action": "nonsense"})
    assert res.status_code == 422


def test_root_app_scheduler_create_toggle_delete():
    """定时任务 CRUD 闭环 (临时任务, 测试后清理)。"""
    res = client.post("/api/v1/scheduler",
                      json={"action": "create", "id": "t_ci_test",
                            "name": "CI 测试任务", "prompt": "测试", "interval_ms": 999999})
    assert res.status_code == 200
    assert res.json().get("status") == "created"

    try:
        res = client.post("/api/v1/scheduler",
                          json={"action": "toggle", "id": "t_ci_test", "enabled": False})
        assert res.status_code == 200
        assert res.json()["status"] == "toggled"

        res = client.post("/api/v1/scheduler", json={"action": "list"})
        ids = [s["id"] for s in res.json()["schedules"]]
        assert "t_ci_test" in ids
    finally:
        client.post("/api/v1/scheduler", json={"action": "delete", "id": "t_ci_test"})


def test_root_app_knowledge_and_mcp():
    """知识库与 MCP 渐进式发现同样挂载 (能力面一致)。"""
    res = client.post("/api/v1/knowledge", json={"action": "list"})
    assert res.status_code == 200
    assert "entries" in res.json()

    res = client.get("/api/v1/mcp/categories")
    assert res.status_code == 200
    assert "servers" in res.json()


def test_root_app_agent_stream_still_works():
    """既有 legacy agent/stream 不受影响 (兼容回归)。"""
    res = client.post("/api/v1/agent/stream", json={"text": "hi", "engine": "master"})
    assert res.status_code == 200
    assert res.headers.get("content-type", "").startswith("text/event-stream")
