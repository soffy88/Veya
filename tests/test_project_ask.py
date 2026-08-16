"""server.project_ask 测试 — 唯一对外入口 (M2 builtin/hicode + M3 dsh adapter)。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server import hicode_queue
from server.project_ask import _decide_assignee, project_ask, project_status, wire_master_tools
from server.project_store import ProjectStore
from server.project_understand import UnderstandResult


def _fake_understand(result: UnderstandResult):
    """构造一个替换 server.project_ask.understand 的桩：忽略入参，直接返回给定结果。"""

    async def _u(request, memory, chain=None, **kwargs):
        return result

    return _u


# ── 派工决策 ─────────────────────────────────────────────────────────


def test_decide_assignee_hint_overrides_heuristic():
    assert _decide_assignee("总结一下现状", "hicode") == "hicode"
    assert _decide_assignee("修复登录 bug", "builtin") == "builtin"
    assert _decide_assignee("总结一下现状", "dsh") == "dsh"


def test_decide_assignee_keyword_heuristic():
    assert _decide_assignee("修复登录页的 bug", None) == "hicode"
    assert _decide_assignee("fix the failing test", None) == "hicode"
    assert _decide_assignee("总结一下现在的项目进度", None) == "builtin"


def test_decide_assignee_heuristic_never_picks_dsh():
    # dsh 只能靠显式 hint 触发, 不参与关键词启发 (新 worker 上线期降低误伤面)
    assert _decide_assignee("修复登录页的 bug", None) != "dsh"
    assert _decide_assignee("随便什么", None) != "dsh"


# ── assignee_hint 白名单门禁: 非法值直接 blocked，不落到启发式/任何 worker ──


@pytest.mark.asyncio
async def test_project_ask_invalid_hint_is_blocked_without_touching_any_worker(
    tmp_path: Path, monkeypatch
):
    async def _boom(*a, **k):
        raise AssertionError("非法 assignee_hint 不应触碰任何 worker")

    monkeypatch.setattr(hicode_queue.hicode_task_queue, "submit", _boom)

    result = await project_ask(str(tmp_path), "随便什么", assignee_hint="codex")
    assert "⛔" in result
    assert "codex" in result

    store = ProjectStore(tmp_path)
    last = store.load_queue_mirror()["tasks"][-1]
    assert last["status"] == "blocked"


@pytest.mark.asyncio
async def test_project_ask_invalid_mode_is_rejected_without_touching_any_worker(
    tmp_path: Path, monkeypatch
):
    async def _boom(*a, **k):
        raise AssertionError("非法 mode 不应触碰任何 worker")

    monkeypatch.setattr(hicode_queue.hicode_task_queue, "submit", _boom)

    result = await project_ask(str(tmp_path), "随便什么", mode="yolo")
    assert "⛔" in result
    assert "mode" in result


# ── Understand 门禁: ask 早退 / act 注入 / parent 续答链 ──────────────────


@pytest.mark.asyncio
async def test_project_ask_understand_ask_exits_early_without_touching_any_worker(
    tmp_path: Path, monkeypatch
):
    """decision=ask 时只追问：不建业务副作用、不派工 (PROJECT_AGENT.md §7)。"""
    import server.project_ask as pa

    async def _boom(*a, **k):
        raise AssertionError("ask 早退不应触碰任何 worker")

    monkeypatch.setattr(hicode_queue.hicode_task_queue, "submit", _boom)
    monkeypatch.setattr(
        pa,
        "understand",
        _fake_understand(
            UnderstandResult(
                decision="ask",
                confidence=0.2,
                interpretation="",
                questions=["要导出全部数据还是只导出当前筛选?"],
            )
        ),
    )

    result = await project_ask(str(tmp_path), "做个导出")
    assert "❓" in result
    assert "要导出全部数据还是只导出当前筛选?" in result

    store = ProjectStore(tmp_path)
    last = store.load_queue_mirror()["tasks"][-1]
    assert last["status"] == "blocked"
    assert last["block_reason"] == "need_clarification"
    assert last["phase"] == "understood_ask"
    # builtin 也不该被触碰：只有一条 DECISIONS 里不该出现这次的 request
    assert "做个导出" not in store.read_decisions()
    # understand.json 落盘, 供续答链读取
    run_dirs = list((store.dir / "runs").iterdir())
    assert (run_dirs[0] / "understand.json").exists()


@pytest.mark.asyncio
async def test_project_ask_understand_ask_fires_structured_event_for_frontend(
    tmp_path: Path, monkeypatch
):
    """主脑的 tool_call 事件不带执行结果 (见 oservi.MasterAgent.chat_stream), 前端要渲染

    澄清卡片得靠这条额外事件, 而不是等模型转述原文。
    """
    import server.project_ask as pa
    from server.events import _on_step_ctx

    monkeypatch.setattr(
        pa,
        "understand",
        _fake_understand(
            UnderstandResult(
                decision="ask",
                confidence=0.2,
                interpretation="",
                questions=["按当前筛选还是全部导出?"],
            )
        ),
    )

    events: list[dict] = []
    token = _on_step_ctx.set(events.append)
    try:
        await project_ask(str(tmp_path), "做个导出")
    finally:
        _on_step_ctx.reset(token)

    matches = [e for e in events if e.get("type") == "project_understand_ask"]
    assert len(matches) == 1
    assert matches[0]["questions"] == ["按当前筛选还是全部导出?"]
    assert matches[0]["task_id"]


@pytest.mark.asyncio
async def test_project_ask_understand_act_injects_interpretation_into_brief(
    tmp_path: Path, monkeypatch
):
    """decision=act 时 interpretation/assumptions 前置进派工 brief (PROJECT_AGENT.md §7.5)。"""
    import server.project_ask as pa

    rec = hicode_queue.TaskRecord(id="tid_u1", spec="x", status="done", summary="done")

    async def _submit(spec, *, workspace=None, meta=None):
        return "tid_u1"

    async def _wait(tid, on_progress=None):
        return rec

    monkeypatch.setattr(hicode_queue.hicode_task_queue, "submit", _submit)
    monkeypatch.setattr(hicode_queue.hicode_task_queue, "wait", _wait)
    monkeypatch.setattr(
        pa,
        "understand",
        _fake_understand(
            UnderstandResult(
                decision="act",
                confidence=0.9,
                interpretation="在 src/export.py 加一个 CSV 导出函数",
                assumptions=["按当前筛选导出"],
            )
        ),
    )

    result = await project_ask(str(tmp_path), "实现导出功能", assignee_hint="hicode")
    assert "✅" in result

    store = ProjectStore(tmp_path)
    run_dirs = list((store.dir / "runs").iterdir())
    brief = (run_dirs[0] / "brief.md").read_text(encoding="utf-8")
    assert "在 src/export.py 加一个 CSV 导出函数" in brief
    assert "按当前筛选导出" in brief


@pytest.mark.asyncio
async def test_project_ask_parent_task_id_chain_reaches_understand(tmp_path: Path, monkeypatch):
    """parent_task_id 续答: 上一轮 understand.json 拼进本轮 understand() 的 chain 入参。"""
    import server.project_ask as pa

    seen: dict = {}

    async def _u(request, memory, chain=None, **kwargs):
        seen["chain"] = chain
        return UnderstandResult(decision="ask", confidence=0.1, interpretation="", questions=["q"])

    monkeypatch.setattr(pa, "understand", _u)

    first = await project_ask(str(tmp_path), "做个导出")
    task_id = first.split("#", 1)[1].split(" ", 1)[0]
    assert seen["chain"] == []  # 第一轮无 parent, chain 为空

    await project_ask(str(tmp_path), "只要当前筛选，CSV", parent_task_id=task_id)

    chain = seen["chain"]
    assert len(chain) == 1
    assert chain[0]["request"] == "做个导出"


# ── builtin 路径: 不得触碰 HicodeTaskQueue ─────────────────────────────


@pytest.mark.asyncio
async def test_project_ask_builtin_records_without_touching_queue(tmp_path: Path, monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("builtin 路径不应调用 HicodeTaskQueue.submit")

    monkeypatch.setattr(hicode_queue.hicode_task_queue, "submit", _boom)

    result = await project_ask(
        str(tmp_path), "更新一下项目状态", assignee_hint="builtin", mode="act_eager"
    )
    assert "✅" in result

    store = ProjectStore(tmp_path)
    assert "更新一下项目状态" in store.read_decisions()
    mirror = store.load_queue_mirror()
    assert mirror["tasks"][-1]["assignee"] == "builtin"
    assert mirror["tasks"][-1]["status"] == "completed"


# ── hicode 路径: 派工 + 状态映射 + 写回 ─────────────────────────────────


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

    result = await project_ask(str(tmp_path), "修复登录 bug", mode="act_eager")
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

    result = await project_ask(str(tmp_path), "fix the failing test", mode="act_eager")
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
    result = await project_ask(str(tmp_path), "修复登录 bug", mode="act_eager")
    assert "⛔" in result
    assert "HICODE_WORKSPACE" in result


# ── dsh 路径: 不可用 / verdict 解析 / 超时 / 无自动 fallback ────────────


@pytest.mark.asyncio
async def test_project_ask_dsh_unavailable_is_blocked(tmp_path: Path, monkeypatch):
    import server.project_ask as pa

    monkeypatch.setattr(pa, "_resolve_dsh_bin", lambda: None)

    async def _boom(*a, **k):
        raise AssertionError("dsh 不可用不该真的 exec 子进程")

    monkeypatch.setattr(pa, "_dsh_exec", _boom)

    result = await project_ask(str(tmp_path), "随便什么", assignee_hint="dsh", mode="act_eager")
    assert "⛔" in result and "not available" in result

    store = ProjectStore(tmp_path)
    last = store.load_queue_mirror()["tasks"][-1]
    assert last["assignee"] == "dsh"
    assert last["status"] == "blocked"


@pytest.mark.asyncio
async def test_project_ask_dsh_completed_parses_verdict(tmp_path: Path, monkeypatch):
    """dsh 若确实输出了 VERDICT 页脚 (非保证行为), 优先按它判定。"""
    import server.project_ask as pa

    monkeypatch.setattr(pa, "_resolve_dsh_bin", lambda: "/usr/bin/dsh")

    async def _fake_exec(bin_path, prompt, cwd, timeout_s):
        assert cwd == str(tmp_path)
        assert "随便什么" in prompt  # headless: 任务是位置参数字符串, 不是文件路径
        return 0, "did some work\nVERDICT: completed\nSUMMARY: fixed it\n", ""

    monkeypatch.setattr(pa, "_dsh_exec", _fake_exec)

    result = await project_ask(str(tmp_path), "随便什么", assignee_hint="dsh", mode="act_eager")
    assert "✅" in result and "fixed it" in result

    store = ProjectStore(tmp_path)
    last = store.load_queue_mirror()["tasks"][-1]
    assert last["assignee"] == "dsh"
    assert last["status"] == "completed"
    # brief 仍完整落盘 (审计), 即便 CLI 调用传的是可能截断过的 prompt 字符串
    run_dirs = list((store.dir / "runs").iterdir())
    assert (run_dirs[0] / "brief.md").exists()


@pytest.mark.asyncio
async def test_project_ask_dsh_blocked_verdict(tmp_path: Path, monkeypatch):
    import server.project_ask as pa

    monkeypatch.setattr(pa, "_resolve_dsh_bin", lambda: "/usr/bin/dsh")

    async def _fake_exec(bin_path, prompt, cwd, timeout_s):
        return 1, "VERDICT: blocked\nSUMMARY: missing credentials\n", ""

    monkeypatch.setattr(pa, "_dsh_exec", _fake_exec)

    result = await project_ask(str(tmp_path), "随便什么", assignee_hint="dsh", mode="act_eager")
    assert "⛔" in result and "missing credentials" in result


@pytest.mark.asyncio
async def test_project_ask_dsh_no_verdict_but_clean_exit_is_completed(tmp_path: Path, monkeypatch):
    """headless 官方形态不保证 VERDICT 页脚 —— exit 0 + 有输出就当 completed。"""
    import server.project_ask as pa

    monkeypatch.setattr(pa, "_resolve_dsh_bin", lambda: "/usr/bin/dsh")

    async def _fake_exec(bin_path, prompt, cwd, timeout_s):
        return 0, "did some work but forgot to print a verdict", ""

    monkeypatch.setattr(pa, "_dsh_exec", _fake_exec)

    result = await project_ask(str(tmp_path), "随便什么", assignee_hint="dsh", mode="act_eager")
    assert "✅" in result and "forgot to print a verdict" in result


@pytest.mark.asyncio
async def test_project_ask_dsh_nonzero_exit_no_verdict_is_blocked_no_fallback(
    tmp_path: Path, monkeypatch
):
    import server.project_ask as pa

    monkeypatch.setattr(pa, "_resolve_dsh_bin", lambda: "/usr/bin/dsh")

    async def _fake_exec(bin_path, prompt, cwd, timeout_s):
        return 1, "", "some real dsh error"

    monkeypatch.setattr(pa, "_dsh_exec", _fake_exec)

    async def _boom(*a, **k):
        raise AssertionError("dsh 失败不应隐式 fallback 到 hicode")

    monkeypatch.setattr(hicode_queue.hicode_task_queue, "submit", _boom)

    result = await project_ask(str(tmp_path), "随便什么", assignee_hint="dsh", mode="act_eager")
    assert "⛔" in result and "some real dsh error" in result


@pytest.mark.asyncio
async def test_project_ask_dsh_timeout_is_blocked(tmp_path: Path, monkeypatch):
    import server.project_ask as pa

    monkeypatch.setattr(pa, "_resolve_dsh_bin", lambda: "/usr/bin/dsh")

    async def _timeout(bin_path, prompt, cwd, timeout_s):
        raise asyncio.TimeoutError

    monkeypatch.setattr(pa, "_dsh_exec", _timeout)

    result = await project_ask(str(tmp_path), "随便什么", assignee_hint="dsh", mode="act_eager")
    assert "⛔" in result and "timed out" in result


def test_parse_dsh_verdict():
    from server.project_ask import _parse_dsh_verdict

    assert _parse_dsh_verdict("blah\nVERDICT: completed\nSUMMARY: ok\n") == ("completed", "ok")
    assert _parse_dsh_verdict("no verdict here") == (None, "")
    assert _parse_dsh_verdict("VERDICT: garbage\n") == (None, "")


# ── project_status: 只读第二入口, 不做派工决策, 不因查询而建目录 ─────────


def test_project_status_uninitialized_project_reports_and_does_not_create_dir(tmp_path: Path):
    result = project_status(str(tmp_path))
    assert "尚不存在" in result
    # 只读: 查询本身不得把 .veya-project/ 建出来
    assert not (tmp_path / ".veya-project").exists()


@pytest.mark.asyncio
async def test_project_status_reflects_prior_project_ask_calls(tmp_path: Path):
    await project_ask(str(tmp_path), "更新一下项目状态", assignee_hint="builtin", mode="act_eager")
    result = project_status(str(tmp_path))
    assert "✅" in result
    assert "builtin" in result
    assert "共 1 条" in result


def test_project_status_limit_caps_recent_entries(tmp_path: Path):
    store = ProjectStore(tmp_path)
    store.ensure_layout()
    mirror = {
        "tasks": [
            {"id": f"t{i}", "assignee": "builtin", "status": "completed", "request": f"req{i}"}
            for i in range(10)
        ]
    }
    store.save_queue_mirror(mirror)
    result = project_status(str(tmp_path), limit=2)
    assert "共 10 条" in result
    assert "最近 2 条" in result
    assert "req9" in result and "req8" in result
    assert "req0" not in result


# ── 单一入口门禁: wire_master_tools 只注册 project_ask + project_status ──


def test_wire_master_tools_registers_only_project_ask_and_status():
    from server.tool_registry import master_tools

    before = set(master_tools.list_tools())
    wire_master_tools()
    after = set(master_tools.list_tools())
    added = after - before
    assert added <= {"project_ask", "project_status"}
    assert "project_ask" in after
    assert "project_status" in after
    # 幂等: 第二次调用不重复注册/不报错
    assert wire_master_tools() == 0
