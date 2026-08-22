"""phase 互斥 (对标"Pi"清单 P1 Harness 生命周期)——hicode CLI 路径接入 SandboxBroker
工作区锁。

真实风险: 两个不同 session 并发调用 hicode_run 走 CLI 兜底路径 (serve 不可达/续做/
force_cli), 会对同一个共享工作目录做未加锁的 git 操作 (`_snapshot_workspace`)、
`hicode` 子进程执行 (`_run_hicode`)、`hicode_rollback` 的 `git reset --hard`——
三者任意两个并发都可能撞 `.git/index.lock`、交错提交, 或者 reset --hard 冲掉
别的会话进行中的改动。`server/hicode_serve.py::run_task` 早就用
`SandboxBroker.async_workspace` 护住了 serve 路径, 这里补的是 CLI 路径。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server import hicode_agent


@pytest.mark.asyncio
async def test_cli_path_serializes_same_workspace(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(hicode_agent, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(hicode_agent, "_resolve_bin", lambda: "/usr/bin/reasonix")
    monkeypatch.setattr(hicode_agent, "_snapshot_workspace", lambda ws, task: None)

    active = 0
    max_active = 0

    async def _fake_run_hicode(
        args, *, workspace, timeout, on_event=None, continue_=False, resume_id=None
    ):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return {"subtype": "success", "is_error": False, "result": "ok", "num_turns": 1}

    monkeypatch.setattr(hicode_agent, "_run_hicode", _fake_run_hicode)

    await asyncio.gather(
        hicode_agent._execute_hicode_core("t1", workspace=str(tmp_path), force_cli=True),
        hicode_agent._execute_hicode_core("t2", workspace=str(tmp_path), force_cli=True),
    )
    assert max_active == 1  # 同一工作区两次并发调用不会同时进临界区


@pytest.mark.asyncio
async def test_cli_path_different_workspaces_run_concurrently(tmp_path: Path, monkeypatch):
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    monkeypatch.setattr(hicode_agent, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(hicode_agent, "_resolve_bin", lambda: "/usr/bin/reasonix")
    monkeypatch.setattr(hicode_agent, "_snapshot_workspace", lambda ws, task: None)

    active = 0
    max_active = 0

    async def _fake_run_hicode(
        args, *, workspace, timeout, on_event=None, continue_=False, resume_id=None
    ):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return {"subtype": "success", "is_error": False, "result": "ok", "num_turns": 1}

    monkeypatch.setattr(hicode_agent, "_run_hicode", _fake_run_hicode)

    await asyncio.gather(
        hicode_agent._execute_hicode_core("t1", workspace=str(ws_a), force_cli=True),
        hicode_agent._execute_hicode_core("t2", workspace=str(ws_b), force_cli=True),
    )
    assert max_active == 2  # 不同工作区互不阻塞 (锁按 workspace key 分桶)


@pytest.mark.asyncio
async def test_rollback_excludes_concurrent_cli_run_same_workspace(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(hicode_agent, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(hicode_agent, "_resolve_bin", lambda: "/usr/bin/reasonix")
    monkeypatch.setattr(hicode_agent, "_snapshot_workspace", lambda ws, task: None)

    active = 0
    max_active = 0

    async def _fake_run_hicode(
        args, *, workspace, timeout, on_event=None, continue_=False, resume_id=None
    ):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return {"subtype": "success", "is_error": False, "result": "ok", "num_turns": 1}

    def _fake_git(ws, *args):
        nonlocal active, max_active

        class _R:
            returncode = 0
            stdout = "deadbeefcafebabe\n"

        if args and args[0] == "rev-parse":
            active += 1
            max_active = max(max_active, active)
            active -= 1
        return _R()

    monkeypatch.setattr(hicode_agent, "_run_hicode", _fake_run_hicode)
    monkeypatch.setattr(hicode_agent, "_git", _fake_git)
    (tmp_path / ".git").mkdir()

    await asyncio.gather(
        hicode_agent._execute_hicode_core("t1", workspace=str(tmp_path), force_cli=True),
        hicode_agent.hicode_rollback(workspace=str(tmp_path)),
    )
    assert max_active == 1  # run 和 rollback 不会同时进临界区


@pytest.mark.asyncio
async def test_serve_path_snapshot_lock_released_before_run_task(tmp_path: Path, monkeypatch):
    """serve 优先路径: 快照那段短锁必须在调 client.run_task 前释放, 否则
    run_task 内部再拿同一把锁会死锁 (顺序 acquire, 不是嵌套)。"""

    class _FakeServeClient:
        async def health(self):
            return True

        async def run_task(self, spec, on_event=None, timeout=900, workspace=None):
            return {"status": "ok", "result": "serve did it"}

    monkeypatch.setattr(hicode_agent, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr("server.hicode_serve.get_serve_client", lambda: _FakeServeClient())
    monkeypatch.setattr(hicode_agent, "_snapshot_workspace", lambda ws, task: None)

    result = await asyncio.wait_for(
        hicode_agent._execute_hicode_core("do something", workspace=str(tmp_path)),
        timeout=5.0,
    )
    assert "serve did it" in result
