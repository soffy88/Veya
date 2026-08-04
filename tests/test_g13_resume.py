"""G13 — checkpoint resume: 从失败分队断点恢复。

Covers:
- handle() 保存初始 checkpoint(原始 command + 完整 squad 计划)
- checkpoint 落盘含 squad 序列化 → resume 确定性重建(不重新调 LLM)
- 失败分队不计入 completed_steps → resume 会重跑它
- resume() 跳过已完成分队、注入 prior outputs、只跑剩余分队
- 旧格式 checkpoint(无 squads 字段)回退为重新拆解
"""

from __future__ import annotations

import json

import pytest

from server.coordinator import Coordinator, SquadTask
from veya.compat import RunState


@pytest.fixture()
def coordinator():
    return Coordinator()


def _complex_text() -> str:
    return "重构整个模块的架构并编写完整测试,涉及多个文件的依赖关系分析" * 3


def _run_state(session_id: str, **kw) -> RunState:
    return RunState(session_id=session_id, **kw)


@pytest.mark.asyncio
async def test_handle_saves_initial_checkpoint(coordinator, tmp_path, monkeypatch):
    """初始 checkpoint(首条 JSONL 记录)携带原始 command + squad 计划。"""
    from server import checkpoint as ckpt_mod

    monkeypatch.setattr(ckpt_mod, "_CKPT_DIR", tmp_path)
    text = _complex_text()
    await coordinator.handle({"text": text, "persona": "build"})

    # 每个 session 一个文件;首条记录 = 初始 checkpoint
    files = list(tmp_path.glob("*.jsonl"))
    assert files
    first = json.loads(files[0].read_text().splitlines()[0])
    payload = first["payload"]
    assert payload["completed_steps"] == []
    assert payload["data"]["command"]["text"] == text
    squads = payload["data"]["squads"]
    assert isinstance(squads, list) and squads
    assert all("squad_id" in s and "role" in s and "command" in s for s in squads)


@pytest.mark.asyncio
async def test_resume_skips_completed_squads(coordinator):
    """completed=[research, plan] → resume 只跑 execute,注入 prior outputs。"""
    squads = [
        SquadTask(squad_id="research", role="research", command={"text": "r"}),
        SquadTask(squad_id="plan", role="plan", command={"text": "p"}, depends_on=["research"]),
        SquadTask(squad_id="execute", role="execute", command={"text": "e"}, depends_on=["plan"]),
    ]
    state = _run_state(
        "sess-resume-1",
        step=2,
        completed_steps=["research", "plan"],
        data={
            "outputs": {"research": "R-out", "plan": "P-out"},
            "command": {"text": _complex_text(), "persona": "build"},
            "squads": [
                {
                    "squad_id": s.squad_id,
                    "role": s.role,
                    "command": s.command,
                    "depends_on": list(s.depends_on),
                }
                for s in squads
            ],
        },
    )

    result = await coordinator.resume(state)
    assert result["resumed_from_step"] == 2
    assert result["resumed_squads"] == ["execute"]  # 只跑剩余分队
    roles = [s["role"] for s in result.get("squads", [])]
    assert "execute" in roles  # 最终汇总包含全部分队


@pytest.mark.asyncio
async def test_failed_squad_not_marked_completed(coordinator, monkeypatch):
    """execute 失败 → checkpoint 的 completed_steps 不含 execute → resume 会重跑。"""
    import pathlib

    from server import checkpoint as ckpt_mod

    monkeypatch.setattr(ckpt_mod, "_CKPT_DIR", pathlib.Path("/tmp/veya-test-g13-fail"))

    real_execute = coordinator._execute_squad

    async def failing_execute(squad, *, session_id):
        if squad.role == "execute":
            return {"status": "failed", "error": "boom"}
        return await real_execute(squad, session_id=session_id)

    monkeypatch.setattr(coordinator, "_execute_squad", failing_execute)

    await coordinator.handle({"text": _complex_text(), "persona": "build"})

    files = sorted(
        (pathlib.Path("/tmp/veya-test-g13-fail")).glob("*.jsonl"), key=lambda p: p.stat().st_mtime
    )
    assert files
    ckpt = await ckpt_mod.load_checkpoint(files[-1].stem)
    assert ckpt is not None
    assert "execute" not in ckpt.payload["completed_steps"]


@pytest.mark.asyncio
async def test_full_round_trip_fail_then_resume(coordinator, tmp_path, monkeypatch):
    """端到端:handle 失败 → load_checkpoint → resume → 成功完成剩余分队。"""
    from server import checkpoint as ckpt_mod

    monkeypatch.setattr(ckpt_mod, "_CKPT_DIR", tmp_path)

    real_execute = coordinator._execute_squad
    calls = {"execute": 0}

    async def flaky_execute(squad, *, session_id):
        if squad.role == "execute":
            calls["execute"] += 1
            if calls["execute"] == 1:
                return {"status": "failed", "error": "transient boom"}
        return await real_execute(squad, session_id=session_id)

    monkeypatch.setattr(coordinator, "_execute_squad", flaky_execute)

    first = await coordinator.handle({"text": _complex_text(), "persona": "build"})
    assert any(s["status"] == "failed" for s in first.get("squads", []))

    files = sorted(tmp_path.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    ckpt = await ckpt_mod.load_checkpoint(files[-1].stem)
    assert ckpt is not None

    from veya.compat import checkpoint_to_run_state

    state = checkpoint_to_run_state(ckpt)
    result = await coordinator.resume(state)
    assert result["resumed_from_step"] >= 1
    assert calls["execute"] == 2  # 第一次失败 + resume 重跑成功
    assert result["resumed_squads"] == ["execute"]  # 只重跑了失败分队


@pytest.mark.asyncio
async def test_resume_legacy_checkpoint_fallback(coordinator, monkeypatch):
    """无 squads 字段的旧 checkpoint → 回退重新拆解,且跳过已完成。"""
    state = _run_state(
        "sess-legacy-1",
        step=1,
        completed_steps=["research"],
        data={
            "outputs": {"research": "old-out"},
            "command": {"text": _complex_text(), "persona": "build"},
            # 无 "squads" 键 → 走 _decompose 回退
        },
    )

    result = await coordinator.resume(state)
    assert result["resumed_from_step"] == 1
    # 回退拆解后的计划至少包含非 research 分队
    roles = [s["role"] for s in result.get("squads", [])]
    assert any(r != "research" for r in roles)


def test_squad_to_dict_roundtrip():
    s = SquadTask(
        squad_id="x", role="plan", command={"text": "hi", "persona": "plan"}, depends_on=["a"]
    )
    from server.coordinator import _squad_to_dict

    d = _squad_to_dict(s)
    restored = SquadTask(**dict(d))
    assert restored.squad_id == "x"
    assert restored.role == "plan"
    assert restored.depends_on == ["a"]
    assert restored.command == {"text": "hi", "persona": "plan"}
