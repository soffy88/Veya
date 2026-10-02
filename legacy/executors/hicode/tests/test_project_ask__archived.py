"""Archived Hicode tests, split out of tests/test_project_ask.py.

Hicode was retired as the project_ask execution backend; the surviving
backends are builtin and dsh. Kept for historical reference only.
"""

@pytest.mark.asyncio
async def test_project_ask_hicode_completed_writes_back(tmp_path: Path, monkeypatch):
    rec = hicode_queue.TaskRecord(id="tid1", spec="x", status="done", summary="did the thing")

    async def _submit(spec, *, workspace=None, meta=None):
        assert workspace == str(tmp_path)
        return "tid1"

    async def _wait(tid, on_progress=None):
        assert tid == "tid1"
        return rec

    monkeypatch.setattr(hicode_queue.hicode_task_queue, "submit", _submit)
    monkeypatch.setattr(hicode_queue.hicode_task_queue, "wait", _wait)

    result = await project_ask(str(tmp_path), "修复登录 bug", executor="hicode", mode="act_eager")
    assert "✅" in result and "did the thing" in result

    store = ProjectStore(tmp_path)
    mirror = store.load_queue_mirror()
    last = mirror["tasks"][-1]
    assert last["assignee"] == "hicode"
    assert last["status"] == "completed"
    # brief 写进了 runs/<task_id>/
    run_dirs = list((store.dir / "runs").iterdir())
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "brief.md").exists()
    assert "修复登录 bug" in (run_dirs[0] / "brief.md").read_text(encoding="utf-8")
@pytest.mark.asyncio
async def test_project_ask_hicode_failed_maps_to_blocked(tmp_path: Path, monkeypatch):
    rec = hicode_queue.TaskRecord(id="tid2", spec="x", status="failed", error="boom")

    async def _submit(spec, *, workspace=None, meta=None):
        return "tid2"

    async def _wait(tid, on_progress=None):
        return rec

    monkeypatch.setattr(hicode_queue.hicode_task_queue, "submit", _submit)
    monkeypatch.setattr(hicode_queue.hicode_task_queue, "wait", _wait)

    result = await project_ask(
        str(tmp_path), "fix the failing test", executor="hicode", mode="act_eager"
    )
    assert "⛔" in result and "boom" in result

    store = ProjectStore(tmp_path)
    assert store.load_queue_mirror()["tasks"][-1]["status"] == "blocked"
@pytest.mark.asyncio
async def test_project_ask_hicode_dispatch_exception_becomes_blocked_not_raised(
    tmp_path: Path, monkeypatch
):
    async def _submit(spec, *, workspace=None, meta=None):
        raise ValueError("workspace 必须位于 HICODE_WORKSPACE 内")

    monkeypatch.setattr(hicode_queue.hicode_task_queue, "submit", _submit)

    # 不应抛异常 —— 必须收敛为 blocked
    result = await project_ask(str(tmp_path), "修复登录 bug", executor="hicode", mode="act_eager")
    assert "⛔" in result
    assert "HICODE_WORKSPACE" in result
