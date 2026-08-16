"""force_cli 门禁: hicode serve 是单一持久会话, 不接受按任务传入的 workspace

(HicodeServeClient.submit 只发 {"input": spec}, 没有 cwd/workspace 参数;
_execute_hicode_core 里传入的 workspace 只用于任务前 git 快照)。project_ask
必须能强制走 CLI (`--add-dir <workspace>`) 才能保证多项目隔离——这是
2026-08-15 真机 smoke 验证发现的真实 gap，不是假设。见 docs/PROJECT_AGENT.md。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server import hicode_agent, hicode_queue


@pytest.mark.asyncio
async def test_force_cli_true_never_touches_serve(tmp_path: Path, monkeypatch):
    def _boom_get_serve_client():
        raise AssertionError("force_cli=True 不应碰 hicode serve")

    monkeypatch.setattr(hicode_agent, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr("server.hicode_serve.get_serve_client", _boom_get_serve_client)
    monkeypatch.setattr(hicode_agent, "_resolve_bin", lambda: "/usr/bin/reasonix")
    monkeypatch.setattr(hicode_agent, "_snapshot_workspace", lambda ws, task: None)

    async def _fake_run_hicode(
        args, *, workspace, timeout, on_event=None, continue_=False, resume_id=None
    ):
        assert str(workspace) == str(tmp_path)
        return {"subtype": "success", "is_error": False, "result": "did it", "num_turns": 1}

    monkeypatch.setattr(hicode_agent, "_run_hicode", _fake_run_hicode)

    summary = await hicode_agent._execute_hicode_core(
        "do something", workspace=str(tmp_path), force_cli=True
    )
    assert "did it" in summary


@pytest.mark.asyncio
async def test_force_cli_false_default_tries_serve_first(tmp_path: Path, monkeypatch):
    """默认行为不变: force_cli=False (缺省) 仍优先尝试 serve —— 不影响既有 hicode_run。"""
    calls = {"serve_health_checked": False}

    class _FakeServeClient:
        async def health(self):
            calls["serve_health_checked"] = True
            return True

        async def run_task(self, spec, on_event=None, timeout=900):
            return {"status": "ok", "result": "serve did it"}

    def _fake_get_serve_client():
        return _FakeServeClient()

    monkeypatch.setattr(hicode_agent, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr("server.hicode_serve.get_serve_client", _fake_get_serve_client)
    monkeypatch.setattr(hicode_agent, "_snapshot_workspace", lambda ws, task: None)

    async def _boom_run_hicode(*a, **k):
        raise AssertionError("默认路径应走 serve, 不该落到 CLI")

    monkeypatch.setattr(hicode_agent, "_run_hicode", _boom_run_hicode)

    await hicode_agent._execute_hicode_core("do something", workspace=str(tmp_path))
    assert calls["serve_health_checked"] is True


@pytest.mark.asyncio
async def test_queue_threads_force_cli_from_meta_to_execute_core(tmp_path: Path, monkeypatch):
    """HicodeTaskQueue._run_one 把 rec.meta['force_cli'] 传给 _execute_hicode_core。"""
    captured = {}

    async def _fake_execute_core(
        spec, workspace=None, timeout_sec=900, on_event=None, force_cli=False
    ):
        captured["force_cli"] = force_cli
        return "✅ done"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", _fake_execute_core)

    queue = hicode_queue.HicodeTaskQueue()
    tid = await queue.submit("task", workspace=str(tmp_path), meta={"force_cli": True})
    rec = await queue.wait(tid)

    assert captured["force_cli"] is True
    assert rec.status == "done"


@pytest.mark.asyncio
async def test_queue_defaults_force_cli_false_when_meta_omits_it(tmp_path: Path, monkeypatch):
    captured = {}

    async def _fake_execute_core(
        spec, workspace=None, timeout_sec=900, on_event=None, force_cli=False
    ):
        captured["force_cli"] = force_cli
        return "✅ done"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", _fake_execute_core)

    queue = hicode_queue.HicodeTaskQueue()
    tid = await queue.submit("task", workspace=str(tmp_path))
    await queue.wait(tid)

    assert captured["force_cli"] is False


@pytest.mark.asyncio
async def test_project_ask_hicode_leg_sets_force_cli_in_submit_meta(tmp_path: Path, monkeypatch):
    """project_ask 的 hicode 路径必须显式 opt-in force_cli, 否则 fix 不生效。"""
    from server.project_ask import project_ask

    captured = {}

    async def _fake_submit(spec, *, workspace=None, meta=None):
        captured["meta"] = meta
        return "tid1"

    rec = hicode_queue.TaskRecord(id="tid1", spec="x", status="done", summary="ok")

    async def _fake_wait(tid, on_progress=None):
        return rec

    monkeypatch.setattr(hicode_queue.hicode_task_queue, "submit", _fake_submit)
    monkeypatch.setattr(hicode_queue.hicode_task_queue, "wait", _fake_wait)

    await project_ask(str(tmp_path), "修复登录 bug", mode="act_eager")

    assert captured["meta"]["force_cli"] is True
