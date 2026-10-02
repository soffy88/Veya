"""server.project_ask tests — the single project entry (builtin / dsh adapters)."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.project_ask import project_ask, project_status, wire_master_tools
from server.project_store import ProjectStore
from server.project_understand import UnderstandResult


def _fake_understand(result: UnderstandResult):
    """构造一个替换 server.project_ask.understand 的桩：忽略入参，直接返回给定结果。"""

    async def _u(request, memory, chain=None, **kwargs):
        return result

    return _u


# ── executor 显式选择门禁 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_project_ask_requires_explicit_executor(tmp_path: Path):
    result = await project_ask(str(tmp_path), "修复登录 bug", mode="act_eager")
    assert "⛔" in result
    assert "executor is required" in result


def test_project_ask_does_not_expose_keyword_router():
    import server.project_ask as pa

    assert not hasattr(pa, "_EXEC_HINTS")
    assert not hasattr(pa, "_decide_assignee")


@pytest.mark.asyncio
async def test_project_ask_invalid_hint_is_blocked_without_touching_any_worker(
    tmp_path: Path, monkeypatch
):
    async def _boom(*a, **k):
        raise AssertionError("非法 assignee_hint 不应触碰任何 worker")

    monkeypatch.setattr("server.project_ask._run_dsh", _boom)

    # "hicode" is retired, so it is now the canonical example of a name this
    # entry must refuse. An admitted executor such as "codex" is no longer a
    # valid negative case.
    result = await project_ask(str(tmp_path), "随便什么", assignee_hint="hicode")
    assert "⛔" in result
    assert "hicode" in result

    store = ProjectStore(tmp_path)
    last = store.load_queue_mirror()["tasks"][-1]
    assert last["status"] == "blocked"


@pytest.mark.asyncio
async def test_project_ask_invalid_mode_is_rejected_without_touching_any_worker(
    tmp_path: Path, monkeypatch
):
    async def _boom(*a, **k):
        raise AssertionError("非法 mode 不应触碰任何 worker")

    monkeypatch.setattr("server.project_ask._run_dsh", _boom)

    result = await project_ask(str(tmp_path), "随便什么", executor="builtin", mode="yolo")
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

    monkeypatch.setattr("server.project_ask._run_dsh", _boom)
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

    result = await project_ask(str(tmp_path), "做个导出", executor="builtin")
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
        await project_ask(str(tmp_path), "做个导出", executor="builtin")
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

    # Hicode is retired; the surviving dispatch leg is the dsh adapter.
    captured: dict[str, str] = {}

    async def _fake_exec(bin_path, prompt, cwd, timeout_s):
        captured["prompt"] = prompt
        return 0, "did some work\nVERDICT: completed\nSUMMARY: fixed it\n", ""

    monkeypatch.setattr(pa, "_resolve_dsh_bin", lambda: "/usr/bin/dsh")
    monkeypatch.setattr(pa, "_dsh_exec", _fake_exec)

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

    # The brief is written by the dispatch leg, so the request must actually
    # reach one. dsh is the surviving executor; Hicode is retired.
    result = await project_ask(str(tmp_path), "实现导出功能", assignee_hint="dsh")
    assert "✅" in result, result

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

    first = await project_ask(str(tmp_path), "做个导出", executor="builtin")
    task_id = first.split("#", 1)[1].split(" ", 1)[0]
    assert seen["chain"] == []  # 第一轮无 parent, chain 为空

    await project_ask(
        str(tmp_path), "只要当前筛选，CSV", executor="builtin", parent_task_id=task_id
    )

    chain = seen["chain"]
    assert len(chain) == 1
    assert chain[0]["request"] == "做个导出"


# ── builtin 路径: 不得触碰 HicodeTaskQueue ─────────────────────────────


@pytest.mark.asyncio
async def test_project_ask_builtin_records_without_touching_queue(tmp_path: Path, monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("builtin 路径不应调用 HicodeTaskQueue.submit")

    monkeypatch.setattr("server.project_ask._run_dsh", _boom)

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

    # A dsh failure must stay blocked. There is no implicit fallback leg left to
    # trip: builtin records and dsh executes, and nothing re-dispatches a failure
    # to a different executor behind the caller's back.
    result = await project_ask(str(tmp_path), "随便什么", assignee_hint="dsh", mode="act_eager")
    assert "⛔" in result and "some real dsh error" in result
    assert "✅" not in result


@pytest.mark.asyncio
async def test_project_ask_dsh_timeout_is_blocked(tmp_path: Path, monkeypatch):
    import server.project_ask as pa

    monkeypatch.setattr(pa, "_resolve_dsh_bin", lambda: "/usr/bin/dsh")

    async def _timeout(bin_path, prompt, cwd, timeout_s):
        raise TimeoutError

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
