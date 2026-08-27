"""Veya Automata 测试 — 定时任务 / 事件触发 / 持久化恢复 / 主脑设闹钟。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from server.automata import VeyaAutomata
from server.coordinator_master import MASTER_SYSTEM_PROMPT, MasterCoordinator
from server.memory_bank import VeyaMemoryBank


@pytest.fixture
def fake_callback():
    """记录合成 Prompt 的假无头执行器。"""
    calls = []

    async def callback(synthetic_prompt: str) -> str:
        calls.append(synthetic_prompt)
        return "HEADLESS DONE: " + synthetic_prompt[:40]

    return callback, calls


def _make_automata(tmp_path, callback, **kwargs):
    return VeyaAutomata(
        callback,
        jobs_db_path=tmp_path / "automata_jobs.json",
        restore_on_start=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Cron 定时任务
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_cron_task(tmp_path, fake_callback):
    callback, calls = fake_callback
    automata = _make_automata(tmp_path, callback)

    msg = automata.register_cron_task("0 9 * * *", "每天早上检查行情")
    assert "自动化任务已就绪" in msg
    assert "cron_" in msg

    jobs = automata.get_jobs()
    assert len(jobs) == 1
    assert jobs[0]["id"].startswith("cron_")
    assert jobs[0]["next_run"] is not None  # 明天早9点

    # 未触发前不执行
    assert calls == []
    automata.shutdown()


@pytest.mark.asyncio
async def test_register_cron_invalid_expression(tmp_path, fake_callback):
    callback, _ = fake_callback
    automata = _make_automata(tmp_path, callback)
    with pytest.raises(ValueError, match="Cron 表达式无效"):
        automata.register_cron_task("99 99 * * *", "任务")
    with pytest.raises(ValueError, match="task_prompt 不能为空"):
        automata.register_cron_task("0 9 * * *", "   ")
    automata.shutdown()


@pytest.mark.asyncio
async def test_persist_and_restore_across_restart(tmp_path, fake_callback):
    """闹钟不因重启丢失: 注册 → 停机 → 新实例恢复。"""
    callback, _ = fake_callback

    automata = _make_automata(tmp_path, callback)
    msg = automata.register_cron_task("30 8 * * 1-5", "工作日早上同步", task_id="cron_workday")
    assert "cron_workday" in msg
    automata.shutdown()

    # 模拟服务器重启: 新实例 restore_on_start=True
    revived = VeyaAutomata(
        callback,
        jobs_db_path=tmp_path / "automata_jobs.json",
        restore_on_start=True,
    )
    jobs = revived.get_jobs()
    assert any(j["id"] == "cron_workday" for j in jobs)
    revived.shutdown()


@pytest.mark.asyncio
async def test_remove_task(tmp_path, fake_callback):
    callback, _ = fake_callback
    automata = _make_automata(tmp_path, callback)
    automata.register_cron_task("0 9 * * *", "任务", task_id="cron_x")
    assert len(automata.get_jobs()) == 1

    msg = automata.remove_task("cron_x")
    assert "已取消" in msg
    assert automata.get_jobs() == []

    msg = automata.remove_task("cron_ghost")
    assert "未找到" in msg
    automata.shutdown()


# ---------------------------------------------------------------------------
# 2. 事件触发 (Event Bus)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_event_runs_headless(tmp_path, fake_callback):
    callback, calls = fake_callback
    automata = _make_automata(tmp_path, callback)

    msg = automata.trigger_event("github_push", {"repo": "veya", "commit": "abc123"})
    assert "已受理" in msg

    # 等待后台异步执行
    for _ in range(50):
        if calls:
            break
        await asyncio.sleep(0.05)
    assert calls, "事件触发后后台任务应执行"
    prompt = calls[0]
    assert "[EVENT TRIGGER: github_push]" in prompt
    assert "github_push" in prompt
    assert "abc123" in prompt

    # 结果留痕
    results = automata.get_recent_results()
    assert len(results) == 1
    assert results[0]["status"] == "success"
    assert "HEADLESS DONE" in results[0]["result"]
    automata.shutdown()


@pytest.mark.asyncio
async def test_headless_mission_hitl_semantics(tmp_path, fake_callback):
    """合成 Prompt 必须携带 HITL 语义(破坏性操作暂停请求人工)。"""
    callback, calls = fake_callback
    automata = _make_automata(tmp_path, callback)

    automata.register_cron_task("0 9 * * *", "删除生产数据库", task_id="cron_hitl")
    # 手动触发一次
    await automata._run_headless_mission("[CRON TRIGGER 0 9 * * *]", "删除生产数据库")
    prompt = calls[0]
    assert "[CRON TRIGGER 0 9 * * *]" in prompt
    assert "Task requirement: 删除生产数据库" in prompt
    assert "HITL (Human-in-the-loop)" in prompt
    automata.shutdown()


@pytest.mark.asyncio
async def test_headless_failure_recorded(tmp_path, fake_callback):
    """后台任务崩溃 → 结果留痕为 failed, 守护进程不崩溃。"""

    async def exploding(prompt):
        raise RuntimeError("LLM 挂了")

    automata = _make_automata(tmp_path, exploding)
    await automata._run_headless_mission("[EVENT TRIGGER: boom]", "任务")
    results = automata.get_recent_results()
    assert results[0]["status"] == "failed"
    assert "RuntimeError" in results[0]["result"]
    automata.shutdown()


# ---------------------------------------------------------------------------
# 3. 主脑集成: 赋予大模型"设闹钟"的权力
# ---------------------------------------------------------------------------


def test_system_prompt_has_automation_rules():
    assert "# AUTOMATION (CRITICAL)" in MASTER_SYSTEM_PROMPT
    assert "system_create_automation" in MASTER_SYSTEM_PROMPT
    assert "Do NOT pretend to run periodic tasks" in MASTER_SYSTEM_PROMPT


def test_system_schemas_include_automation_tools(tmp_path):
    automata = MagicMock()
    coord = MasterCoordinator(
        memory_bank=VeyaMemoryBank(storage_path=tmp_path / "m.json"), automata=automata
    )
    names = {s["function"]["name"] for s in coord.get_system_schemas()}
    assert "system_create_automation" in names
    assert "system_remove_automation" in names
    assert "system_list_automations" in names
    # create 工具 schema 完整
    create_schema = next(
        s for s in coord.get_system_schemas() if s["function"]["name"] == "system_create_automation"
    )
    assert create_schema["function"]["parameters"]["required"] == ["cron_expr", "task_prompt"]


@pytest.mark.asyncio
async def test_handle_tool_call_automation_routing(tmp_path):
    """主脑拦截自动化指令 → 路由到 Automata 引擎。"""
    callback, _ = fake_callback_async()
    automata = _make_automata(tmp_path, callback)
    coord = MasterCoordinator(
        memory_bank=VeyaMemoryBank(storage_path=tmp_path / "m.json"), automata=automata
    )

    out = await coord.handle_tool_call(
        "system_create_automation", {"cron_expr": "0 9 * * *", "task_prompt": "每日行情"}
    )
    assert "自动化任务已就绪" in out
    assert len(automata.get_jobs()) == 1

    task_id = automata.get_jobs()[0]["id"]
    out = await coord.handle_tool_call("system_remove_automation", {"task_id": task_id})
    assert "已取消" in out

    out = await coord.handle_tool_call("system_list_automations", {})
    assert "当前没有后台自动化任务" in out
    automata.shutdown()


def fake_callback_async():
    calls = []

    async def cb(prompt):
        calls.append(prompt)
        return "ok"

    return cb, calls


@pytest.mark.asyncio
async def test_full_loop_llm_creates_automation(tmp_path):
    """完整闭环: 模型决定设闹钟 → system_create_automation → 任务落盘。"""
    callback, _ = fake_callback_async()
    automata = _make_automata(tmp_path, callback)
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "system_create_automation",
                                        "arguments": json.dumps(
                                            {
                                                "cron_expr": "0 9 * * *",
                                                "task_prompt": "每天早上检查比特币价格并总结",
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            }
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "已设置每日 9 点的自动任务。"}}
            ],
            "usage": {},
        }

    coord = MasterCoordinator(
        memory_bank=VeyaMemoryBank(storage_path=tmp_path / "m.json"),
        automata=automata,
        llm_fn=fake_llm,
        max_rounds=3,
    )
    result = await coord.chat_stream("每天早上 9 点帮我检查比特币价格", session_id="auto1")

    assert result["status"] == "success"
    assert result["tool_calls"][0]["tool"] == "system_create_automation"
    # 注册结果回喂
    assert "自动化任务已就绪" in calls[1][-1]["content"]
    # 任务真实落盘
    assert len(automata.get_jobs()) == 1
    # 持久化文件存在(重启不丢)
    assert (tmp_path / "automata_jobs.json").exists()
    automata.shutdown()


# ---------------------------------------------------------------------------
# 4. FastAPI 网关集成
# ---------------------------------------------------------------------------


def test_automata_routes_reachable():
    """/automata/jobs 与 /api/v1/webhooks/{source} 已挂载。"""
    from fastapi.testclient import TestClient

    from server.app import app
    from server.automata import reset_automata

    try:
        with TestClient(app) as client:  # with 触发 lifespan(守护进程正常启停)
            resp = client.get("/automata/jobs")
            assert resp.status_code == 200
            assert "jobs" in resp.json()

            resp = client.post("/api/v1/webhooks/github", json={"ref": "main", "commit": "x"})
            assert resp.status_code == 200
            assert "Event received" in resp.json()["status"]
    finally:
        reset_automata()
