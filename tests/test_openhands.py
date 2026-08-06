"""OpenHands 对标门禁 — ACP 客户端 / 多 backend 挂载 / Issue 自动拆解。"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.acp_client import ACPBackend
from server.backends import BackendRegistry

# =========================================================================
# Mock ACP 服务器 (子进程, JSON-RPC over stdio)
# =========================================================================

MOCK_ACP_SERVER = textwrap.dedent("""
    import json, sys

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    sessions = set()
    for line in sys.stdin:
        msg = json.loads(line)
        m, rid = msg.get("method"), msg.get("id")
        params = msg.get("params", {})
        if m == "session/new":
            sid = "mock-session-1"
            sessions.add(sid)
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": sid}})
        elif m == "session/init":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif m == "task/start":
            tid = params.get("taskId", "t1")
            send({"jsonrpc": "2.0", "id": rid, "result": {"taskId": tid}})
            # 文本事件 + 结束事件
            send({"jsonrpc": "2.0", "method": "task/event", "params": {
                "taskId": tid,
                "event": {"type": "text", "content": {"text": "来自 mock ACP agent 的回复"}}
            }})
            send({"jsonrpc": "2.0", "method": "task/event", "params": {
                "taskId": tid, "event": {"type": "task/end", "ok": True}
            }})
        elif m == "task/cancel":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif m == "session/close":
            sessions.discard(params.get("sessionId", ""))
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
""")

MOCK_ACP_PATH = Path("/tmp/mock_acp_server.py")
if not MOCK_ACP_PATH.exists():
    MOCK_ACP_PATH.write_text(MOCK_ACP_SERVER)


@pytest.mark.asyncio
async def test_acp_backend_full_run():
    """ACP 客户端: session/new → init → task/start → 聚合文本事件。"""
    backend = ACPBackend([sys.executable, str(MOCK_ACP_PATH)], agent="general")
    result = await backend.run("修复登录 bug", timeout_s=30)
    assert result["ok"] is True
    assert "mock ACP agent" in result["output"]
    assert result["events"] >= 2
    await backend.close()


@pytest.mark.asyncio
async def test_acp_backend_missing_process():
    """ACP 进程不存在 → 结构化错误而非崩溃。"""
    from server.acp_client import ACPError

    backend = ACPBackend(["/no/such/acp-binary-xyz"])
    with pytest.raises(ACPError):
        await backend.run("hi", timeout_s=5)
    await backend.close()


# =========================================================================
# BackendRegistry — 多 backend 挂载
# =========================================================================

def test_registry_discover_and_register():
    reg = BackendRegistry()
    names = {b["name"] for b in reg.list()}
    assert "master" in names          # builtin 恒在
    # cli 按本机探测 (有则有)
    for eng in ("claude", "codex", "pi"):
        if __import__("shutil").which(eng):
            assert eng in names

    reg.register("my-acp", "acp", command=["acp-agent"], agent="general")
    assert any(b["name"] == "my-acp" and b["kind"] == "acp" for b in reg.list())

    with pytest.raises(ValueError):
        reg.register("bad", "unknown-kind")


@pytest.mark.asyncio
async def test_registry_run_missing_backend_and_unavailable():
    reg = BackendRegistry()
    with pytest.raises(KeyError):
        await reg.run("no-such-backend", "hi")

    # cli 后端但二进制不存在 → 结构化错误
    reg.register("ghost-cli", "cli", command=["definitely-not-installed-xyz"])
    result = await reg.run("ghost-cli", "hi", timeout_s=10)
    assert result["ok"] is False
    assert "不可用" in result["error"]


@pytest.mark.asyncio
async def test_registry_run_acp_backend():
    reg = BackendRegistry()
    reg.register("mock-acp", "acp", command=[sys.executable, str(MOCK_ACP_PATH)])
    result = await reg.run("mock-acp", "写一个测试", timeout_s=30)
    assert result["ok"] is True
    assert "mock ACP agent" in result["output"]


def test_registry_status_aggregation():
    reg = BackendRegistry()
    statuses = reg.status()
    master = next(s for s in statuses if s["name"] == "master")
    assert master["available"] is True
    assert "running_tasks" in master and "busy" in master


# =========================================================================
# API
# =========================================================================

def test_backends_api_list_status_register():
    client = TestClient(__import__("server.app", fromlist=["app"]).app)

    r = client.get("/api/v1/backends")
    assert r.status_code == 200
    assert any(b["name"] == "master" for b in r.json()["backends"])

    r = client.get("/api/v1/backends/status")
    assert r.status_code == 200
    assert r.json()["backends"][0]["available"] is True

    r = client.post("/api/v1/backends/register",
                    json={"name": "api-acp", "kind": "acp", "command": ["x-agent"]})
    assert r.status_code == 200 and r.json()["status"] == "registered"

    # 非法 kind → 400
    r = client.post("/api/v1/backends/register",
                    json={"name": "bad", "kind": "nope"})
    assert r.status_code == 400


def test_backends_run_unknown_404():
    client = TestClient(__import__("server.app", fromlist=["app"]).app)
    r = client.post("/api/v1/backends/run",
                    json={"name": "ghost", "prompt": "hi"})
    assert r.status_code == 404


# =========================================================================
# Issue 自动拆解
# =========================================================================

def test_decompose_checklist_and_headings():
    from server.routes.backends import decompose_issue

    tasks = decompose_issue("- [ ] 修复登录页\n- [x] 加测试\n- [ ] 发版", "登录 bug")
    assert [t["title"] for t in tasks] == ["修复登录页", "加测试", "发版"]

    tasks2 = decompose_issue("## 后端\n### 缓存\n## 前端", "重构")
    assert len(tasks2) == 3

    tasks3 = decompose_issue("一句话 issue", "简单任务")
    assert len(tasks3) == 1


def test_issue_decompose_api_no_github():
    """直接 body 拆解: 创建看板 + 线性依赖链 + auto_start=False 不启动。"""
    client = TestClient(__import__("server.app", fromlist=["app"]).app)
    r = client.post("/api/v1/automation/issue-decompose", json={
        "body": "- [ ] 任务一\n- [ ] 任务二\n- [ ] 任务三",
        "title": "测试 issue",
        "board": "test-issue-board",
        "auto_start": False,
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "decomposed"
    assert data["tasks"] == 3
    assert data["chain"] == "linear"
    assert len(data["cards"]) == 3
    assert data["started"] == []

    # 看板状态: 三卡 todo + 线性依赖
    r2 = client.post("/api/v1/board", json={"action": "status", "board": "test-issue-board"})
    cards = {c["id"]: c for c in r2.json()["cards"]}
    ids = data["cards"]
    assert all(cards[i]["status"] == "todo" for i in ids)
    assert cards[ids[1]]["depends_on"] == [ids[0]]
    assert cards[ids[2]]["depends_on"] == [ids[1]]


def test_issue_decompose_requires_content():
    client = TestClient(__import__("server.app", fromlist=["app"]).app)
    r = client.post("/api/v1/automation/issue-decompose", json={})
    assert r.status_code == 400
