"""Kanban 编排门禁 — worktree 隔离 + 看板状态机 + 依赖链自动触发。

对标 Cline Kanban 三机制: 隔离 (worktree) / 收敛 (auto-commit) / 流水线 (依赖链)。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.board import BoardStore, BoardWorker, GitWorktree

# =========================================================================
# BoardStore — 状态机与依赖
# =========================================================================


def test_board_crud_and_status_machine(tmp_path):
    store = BoardStore(tmp_path / "boards.json")
    store.create("b1", repo=str(tmp_path / "repo"))
    assert store.get("b1") is not None

    card = store.add_card("b1", title="任务A", prompt="做 A")
    assert card.status == "todo"
    assert card.id.startswith("c_")

    # 重复创建报错
    with pytest.raises(ValueError):
        store.create("b1")

    # 依赖
    b = store.add_card("b1", title="任务B", prompt="做 B", depends_on=[card.id])
    assert store.pending_dependencies("b1", b.id) == [card.id]
    assert store.dependencies_satisfied("b1", b.id) is False
    assert store.downstream("b1", card.id) == [b.id]

    # 依赖完成后满足 (依赖卡 card 置 done)
    store.get("b1").cards[card.id].status = "done"
    store.save()
    assert store.dependencies_satisfied("b1", b.id) is True

    # 持久化往返
    store2 = BoardStore(tmp_path / "boards.json")
    assert store2.get("b1").cards[card.id].title == "任务A"


# =========================================================================
# GitWorktree — 真实 git 隔离/收敛/审查
# =========================================================================


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_git_worktree_isolate_commit_diff_cleanup(git_repo):
    wt = GitWorktree(str(git_repo))
    branch = "card-test-1"
    path = wt.add(branch)
    assert Path(path).exists()

    # 隔离: worktree 内写文件不影响主工作树
    (Path(path) / "feature.txt").write_text("feature\n")
    assert not (git_repo / "feature.txt").exists()

    # 收敛: auto-commit 到独立分支
    sha = wt.commit(path, "card test")
    assert len(sha) >= 7
    r = subprocess.run(
        ["git", "branch", "--show-current"], cwd=path, capture_output=True, text=True
    )
    assert r.stdout.strip() == branch

    # 审查: diff 统计可见
    d = wt.diff_stat(wt=path)
    assert "feature.txt" in d["stat"]

    # 清理
    wt.remove(path)
    assert not Path(path).exists()


# =========================================================================
# BoardWorker — 依赖链自动触发
# =========================================================================


@pytest.mark.asyncio
async def test_dependency_chain_auto_trigger(tmp_path, monkeypatch):
    store = BoardStore(tmp_path / "boards.json")
    worker = BoardWorker(store)

    async def fake_execute(self, board, card):
        card.status = "done"
        card.commit_sha = "deadbeef"
        card.exit_code = 0

    monkeypatch.setattr(BoardWorker, "_execute", fake_execute)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    store.create("b1", repo=str(repo))
    a = store.add_card("b1", title="A", prompt="A", engine="claude")
    b = store.add_card("b1", title="B", prompt="B", engine="claude", depends_on=[a.id])

    # B 依赖 A 未完成 → 不能启动
    with pytest.raises(ValueError):
        await worker.start_card("b1", b.id)

    # A 启动 → 完成 → trash → B 自动启动
    await worker.start_card("b1", a.id)
    assert store.get("b1").cards[a.id].status == "running"
    await asyncio.sleep(0.05)
    assert store.get("b1").cards[a.id].status == "done"

    triggered = await worker.trash_card("b1", a.id)
    assert b.id in triggered
    assert store.get("b1").cards[b.id].status == "running"

    # 未完成卡不能 trash
    with pytest.raises(ValueError):
        await worker.trash_card("b1", b.id)


@pytest.mark.asyncio
async def test_start_requires_repo_and_blocked_by_dependency(tmp_path):
    store = BoardStore(tmp_path / "boards.json")
    worker = BoardWorker(store)
    store.create("b1")  # 无 repo
    a = store.add_card("b1", title="A", prompt="A")
    with pytest.raises(ValueError):
        await worker.start_card("b1", a.id)


# =========================================================================
# API — 看板端点
# =========================================================================


@pytest.mark.asyncio
async def test_board_api_flow(tmp_path, monkeypatch):
    from server import board as board_mod

    store = BoardStore(tmp_path / "boards.json")
    worker = BoardWorker(store)
    monkeypatch.setattr(board_mod, "_default_store", store)
    monkeypatch.setattr(board_mod, "_default_worker", worker)

    async def fake_execute(self, b, card):
        card.status = "done"
        card.commit_sha = "abc123"

    monkeypatch.setattr(BoardWorker, "_execute", fake_execute)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    client = TestClient(__import__("server.app", fromlist=["app"]).app)

    r = client.post("/api/v1/board", json={"action": "create", "name": "b1", "repo": str(repo)})
    assert r.status_code == 200 and r.json()["status"] == "created"

    r = client.post(
        "/api/v1/board", json={"action": "add_card", "board": "b1", "title": "A", "prompt": "做 A"}
    )
    aid = r.json()["card_id"]

    r = client.post(
        "/api/v1/board",
        json={
            "action": "add_card",
            "board": "b1",
            "title": "B",
            "prompt": "做 B",
            "depends_on": [aid],
        },
    )
    bid = r.json()["card_id"]

    # 依赖未满足 → start 400
    r = client.post("/api/v1/board", json={"action": "start", "board": "b1", "card_id": bid})
    assert r.status_code == 400

    # A start → done → trash → B 自动启动
    r = client.post("/api/v1/board", json={"action": "start", "board": "b1", "card_id": aid})
    assert r.status_code == 200
    await asyncio.sleep(0.05)
    r = client.post("/api/v1/board", json={"action": "trash", "board": "b1", "card_id": aid})
    assert bid in r.json()["triggered"]

    r = client.post("/api/v1/board", json={"action": "status", "board": "b1"})
    statuses = {c["id"]: c["status"] for c in r.json()["cards"]}
    assert statuses[aid] == "trash"
    assert statuses[bid] in ("running", "done")  # fake 执行可能已同步完成

    # 列表
    r = client.post("/api/v1/board", json={"action": "list"})
    assert any(b["name"] == "b1" for b in r.json()["boards"])


def test_board_api_unknown_action():
    from fastapi.testclient import TestClient

    from server.app import app

    r = TestClient(app).post("/api/v1/board", json={"action": "nonsense"})
    assert r.status_code == 422
