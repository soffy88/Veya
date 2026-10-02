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

        async def run_task(self, spec, on_event=None, timeout=900, **kwargs):
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
        spec, workspace=None, timeout_sec=900, on_event=None, force_cli=False, **kwargs
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
        spec, workspace=None, timeout_sec=900, on_event=None, force_cli=False, **kwargs
    ):
        captured["force_cli"] = force_cli
        return "✅ done"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", _fake_execute_core)

    queue = hicode_queue.HicodeTaskQueue()
    tid = await queue.submit("task", workspace=str(tmp_path))
    await queue.wait(tid)

    assert captured["force_cli"] is False


@pytest.mark.asyncio
async def test_queue_persists_terminal_goalrun_projection(tmp_path: Path, monkeypatch):
    """Hicode queue is a projection over one real GoalRun, not its owner."""
    import json

    async def _fake_execute_core(
        spec, workspace=None, timeout_sec=900, on_event=None, force_cli=False, **kwargs
    ):
        return "provider completed"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", _fake_execute_core)
    queue = hicode_queue.HicodeTaskQueue()
    tid = await queue.submit("read-only proof", workspace=str(tmp_path))
    rec = await queue.wait(tid)

    assert rec.status == "done"
    goal_id = rec.meta.get("goal_id")
    assert goal_id
    graph = tmp_path / ".veya-project" / "goal-runs" / goal_id / "taskgraph.json"
    data = json.loads(graph.read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["tasks"][0]["id"] == f"hicode:{tid}"
    assert data["tasks"][0]["status"] == "completed"


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

    await project_ask(str(tmp_path), "修复登录 bug", executor="hicode", mode="act_eager")

    assert captured["meta"]["force_cli"] is True


@pytest.mark.asyncio
async def test_hicode_resume_routes_through_goalrun_queue(tmp_path: Path, monkeypatch):
    captured = {}

    async def _fake_submit(spec, *, workspace=None, meta=None):
        captured["spec"] = spec
        captured["workspace"] = workspace
        captured["meta"] = dict(meta or {})
        return "resume1"

    rec = hicode_queue.TaskRecord(
        id="resume1",
        spec="continue work",
        status="done",
        summary="resumed",
    )

    async def _fake_wait(tid, on_progress=None):
        return rec

    monkeypatch.setattr(hicode_queue.hicode_task_queue, "submit", _fake_submit)
    monkeypatch.setattr(hicode_queue.hicode_task_queue, "wait", _fake_wait)

    result = await hicode_agent.hicode_run(
        "continue work",
        workspace=str(tmp_path),
        max_steps=7,
        timeout_sec=123,
        session_id="machine-42",
        continue_=False,
    )

    assert result == "resumed"
    assert captured["meta"]["session_id"] == "machine-42"
    assert captured["meta"]["max_steps"] == 7
    assert captured["meta"]["timeout_sec"] == 123
    assert captured["meta"]["force_cli"] is True


@pytest.mark.asyncio
async def test_queue_persists_resume_envelope_and_forwards_meta(tmp_path: Path, monkeypatch):
    captured = {}

    async def _fake_execute_core(spec, **kwargs):
        captured.update(kwargs)
        return "provider completed"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", _fake_execute_core)
    queue = hicode_queue.HicodeTaskQueue()
    tid = await queue.submit(
        "resume proof",
        workspace=str(tmp_path),
        meta={
            "session_id": "session-9",
            "continue_": True,
            "max_steps": 11,
            "timeout_sec": 77,
            "force_cli": True,
        },
    )
    rec = await queue.wait(tid)

    assert rec.status == "done"
    assert captured["session_id"] == "session-9"
    assert captured["continue_"] is True
    assert captured["max_steps"] == 11
    goal_id = rec.meta["goal_id"]
    graph = tmp_path / ".veya-project" / "goal-runs" / goal_id / "taskgraph.json"
    import json

    data = json.loads(graph.read_text(encoding="utf-8"))
    spec, meta = hicode_queue._decode_goal_instruction(data["tasks"][0]["instruction"])
    assert spec == "resume proof"
    assert meta["session_id"] == "session-9"
    assert meta["continue_"] is True
    assert meta["max_steps"] == 11


@pytest.mark.asyncio
async def test_recovery_restores_hicode_resume_meta_and_project_root(tmp_path: Path, monkeypatch):
    goal_dir = tmp_path / ".veya-project" / "goal-runs" / "goal-1"
    goal_dir.mkdir(parents=True)
    import json

    instruction = hicode_queue._encode_goal_instruction(
        "resume after crash",
        {
            "session_id": "session-crash",
            "continue_": True,
            "max_steps": 13,
            "timeout_sec": 88,
            "force_cli": True,
        },
    )
    (goal_dir / "taskgraph.json").write_text(
        json.dumps(
            {
                "status": "running",
                "tasks": [{"id": "hicode:recover1", "instruction": instruction}],
            }
        ),
        encoding="utf-8",
    )
    queue = hicode_queue.HicodeTaskQueue()
    monkeypatch.setattr(queue, "_ensure_worker", lambda: None)

    recovered = await queue.recover_goal_runs(str(tmp_path))
    rec = queue.get("recover1")

    assert recovered == 1
    assert rec is not None
    assert rec.workspace == str(tmp_path.resolve())
    assert rec.spec == "resume after crash"
    assert rec.meta["session_id"] == "session-crash"
    assert rec.meta["continue_"] is True
    assert rec.meta["max_steps"] == 13


@pytest.mark.asyncio
async def test_noncompleted_goalrun_cannot_leave_hicode_running(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    async def _fake_project_run_goal(**kwargs):
        return SimpleNamespace(
            status="blocked",
            block_reason="verification blocked",
            goal_id="goal-blocked",
        )

    monkeypatch.setattr("server.goal_run.runner.project_run_goal", _fake_project_run_goal)
    queue = hicode_queue.HicodeTaskQueue()
    rec = hicode_queue.TaskRecord(
        id="blocked1",
        spec="never executed",
        status="running",
        workspace=str(tmp_path),
    )

    await queue._run_one(rec)

    assert rec.status == "failed"
    assert rec.error == "verification blocked"


@pytest.mark.asyncio
async def test_stop_uses_owned_cli_process_record_before_serve(tmp_path: Path, monkeypatch):
    queue = hicode_queue.HicodeTaskQueue()
    rec = hicode_queue.TaskRecord(
        id="cli1",
        spec="resume",
        status="running",
        workspace=str(tmp_path),
        meta={"process_record": str(tmp_path / "cli.pid.json")},
    )
    queue._tasks[rec.id] = rec

    def _fake_terminate(path, workspace=""):
        rec._done.set()
        return {"killed": 1, "pids": [12345], "refused": None}

    def _boom_serve():
        raise AssertionError("owned CLI process was killed; serve cancel must not be used")

    monkeypatch.setattr("server.exec_process.terminate", _fake_terminate)
    monkeypatch.setattr("server.hicode_serve.get_serve_client", _boom_serve)

    assert await queue.stop(rec.id) is True
    assert rec.cancel_requested is True
