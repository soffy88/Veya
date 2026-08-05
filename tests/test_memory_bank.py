"""跨会话潜意识测试 — 全局偏好账本 / 隐式捕捉 / 潜意识注入。"""

from __future__ import annotations

import json

import pytest

from server.coordinator_master import MASTER_SYSTEM_PROMPT, MasterCoordinator
from server.memory_bank import VeyaMemoryBank


@pytest.fixture
def bank(tmp_path) -> VeyaMemoryBank:
    return VeyaMemoryBank(storage_path=tmp_path / "global_memory.json")


# ---------------------------------------------------------------------------
# 1. 账本读写
# ---------------------------------------------------------------------------


def test_add_and_persist_preference(tmp_path):
    bank = VeyaMemoryBank(storage_path=tmp_path / "mem.json")
    out = bank.add_preference("Always use pnpm instead of npm", "Coding")
    assert "永久记忆已更新" in out
    assert "pnpm" in out

    prefs = bank.list_preferences()
    assert len(prefs) == 1
    assert prefs[0]["rule"] == "Always use pnpm instead of npm"
    assert prefs[0]["context"] == "Coding"
    assert prefs[0]["id"].startswith("mem_")

    # 重启实例 → 恢复(跨会话)
    revived = VeyaMemoryBank(storage_path=tmp_path / "mem.json")
    assert len(revived.list_preferences()) == 1
    assert revived.list_preferences()[0]["rule"].startswith("Always use pnpm")


def test_add_preference_deduplication(bank):
    bank.add_preference("Use port 8080", "Coding")
    out = bank.add_preference("Use port 8080", "Coding")
    assert "already exists" in out
    assert len(bank.list_preferences()) == 1

    # 同 rule 不同 context → 仍算重复(rule 是唯一键)
    out = bank.add_preference("Use port 8080", "Architecture")
    assert "already exists" in out
    assert len(bank.list_preferences()) == 1


def test_add_empty_rule_rejected(bank):
    out = bank.add_preference("   ", "Coding")
    assert "不能为空" in out
    assert len(bank.list_preferences()) == 0


def test_remove_preference(bank):
    bank.add_preference("Rule A", "Tone")
    bank.add_preference("Rule B", "Tone")
    mem_id = bank.list_preferences()[0]["id"]

    out = bank.remove_preference(mem_id)
    assert "已擦除" in out
    assert len(bank.list_preferences()) == 1

    out = bank.remove_preference("mem_nonexistent")
    assert "未找到" in out
    assert len(bank.list_preferences()) == 1


def test_preference_capacity_cap(tmp_path):
    bank = VeyaMemoryBank(storage_path=tmp_path / "mem.json")
    # 塞满上限 + 多一条
    for i in range(205):
        bank.add_preference(f"Rule number {i}", "Stress")
    prefs = bank.list_preferences()
    assert len(prefs) <= 200  # 容量上限
    # 最旧的被裁剪, 最新的保留
    assert not any(p["rule"] == "Rule number 0" for p in prefs)
    assert any(p["rule"] == "Rule number 204" for p in prefs)


def test_id_uniqueness(bank):
    for i in range(10):
        bank.add_preference(f"rule-{i}", "T")
    stored = [p["id"] for p in bank.list_preferences()]
    assert len(set(stored)) == 10  # ID 永不重复(时间戳 + 随机后缀)


def test_search_and_stats(bank):
    bank.add_preference("Always use pnpm", "Coding")
    bank.add_preference("Keep responses concise", "Tone")
    hits = bank.search_preferences("pnpm")
    assert len(hits) == 1 and hits[0]["context"] == "Coding"
    stats = bank.get_stats()
    assert stats["count"] == 2
    assert stats["contexts"] == ["Coding", "Tone"]


# ---------------------------------------------------------------------------
# 2. 潜意识注入
# ---------------------------------------------------------------------------


def test_inject_subconscious_empty(bank):
    assert bank.inject_subconscious() == ""


def test_inject_subconscious_format(bank):
    bank.add_preference("Always use pnpm instead of npm", "Coding")
    bank.add_preference("My standard port is 8080", "Architecture")

    prompt = bank.inject_subconscious()
    assert "[YOUR SUBCONSCIOUS MEMORY & USER PREFERENCES]" in prompt
    assert "You MUST strictly obey the following rules across all sessions" in prompt
    assert "- [Coding] Always use pnpm instead of npm (ID: mem_" in prompt
    assert "- [Architecture] My standard port is 8080 (ID: mem_" in prompt


def test_memory_bank_file_structure(tmp_path):
    bank = VeyaMemoryBank(storage_path=tmp_path / "mem.json")
    bank.add_preference("Rule", "T")
    data = json.loads((tmp_path / "mem.json").read_text(encoding="utf-8"))
    assert "preferences" in data
    assert data["preferences"][0]["rule"] == "Rule"


# ---------------------------------------------------------------------------
# 3. 主脑集成: 隐式捕捉 + 潜意识生效
# ---------------------------------------------------------------------------


def _tool_response(name: str, args: dict, content: str = "", tc_id: str = "call_1") -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": tc_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _text_response(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": {}}


def test_system_prompt_has_memory_management_rules():
    """System Prompt 下达死命令: 被纠正必须静默记笔记。"""
    assert "# MEMORY MANAGEMENT (CRITICAL)" in MASTER_SYSTEM_PROMPT
    assert "system_save_preference" in MASTER_SYSTEM_PROMPT
    assert "Do not ask for permission" in MASTER_SYSTEM_PROMPT


def test_system_schemas_include_memory_tools(tmp_path):
    coord = MasterCoordinator(memory_bank=VeyaMemoryBank(storage_path=tmp_path / "m.json"))
    names = {s["function"]["name"] for s in coord.get_system_schemas()}
    assert "system_save_preference" in names
    assert "system_remove_preference" in names
    assert "system_reload_skills" in names
    # save 工具的 schema 完整(rule/context 必填)
    save_schema = next(s for s in coord.get_system_schemas() if s["function"]["name"] == "system_save_preference")
    assert save_schema["function"]["parameters"]["required"] == ["rule", "context"]


def test_subconscious_injected_into_system_prompt(tmp_path):
    bank = VeyaMemoryBank(storage_path=tmp_path / "m.json")
    bank.add_preference("Always use pnpm instead of npm", "Coding")
    coord = MasterCoordinator(memory_bank=bank)
    prompt = coord.get_system_prompt()
    assert "[YOUR SUBCONSCIOUS MEMORY & USER PREFERENCES]" in prompt
    assert "[Coding] Always use pnpm instead of npm (ID: mem_" in prompt


@pytest.mark.asyncio
async def test_cross_session_memory_lifecycle(tmp_path):
    """【魔幻时刻】会话1 纠正 → 落盘; 会话2 潜意识生效。"""
    bank = VeyaMemoryBank(storage_path=tmp_path / "m.json")

    # ── 会话 1 (周一): 用户纠正 → 主脑静默保存 ──
    calls1 = []

    async def fake_llm_1(messages, **kwargs):
        calls1.append(list(messages))
        if len(calls1) == 1:
            return _tool_response(
                "system_save_preference",
                {"rule": "Always use pnpm for package management, never npm", "context": "Coding"},
            )
        return _text_response("收到, 已将您的包管理工具偏好永久记录为 pnpm。")

    coord1 = MasterCoordinator(llm_fn=fake_llm_1, memory_bank=bank, max_rounds=3)
    r1 = await coord1.chat_stream("不对, 我这里统一用 pnpm, 你以后别提 npm 了", session_id="mon")
    assert r1["status"] == "success"
    assert r1["tool_calls"][0]["tool"] == "system_save_preference"
    # 账本已落盘
    assert len(bank.list_preferences()) == 1
    assert "pnpm" in bank.list_preferences()[0]["rule"]

    # ── 会话 2 (周三): 新实例, 账本从硬盘恢复 → 潜意识注入 ──
    revived_bank = VeyaMemoryBank(storage_path=tmp_path / "m.json")
    coord2 = MasterCoordinator(llm_fn=lambda m, **k: _text_response("ok"), memory_bank=revived_bank)

    prompt = coord2.get_system_prompt()
    assert "[Coding] Always use pnpm for package management, never npm (ID: mem_" in prompt
    # 会话2 的 system 消息里带着周一的规矩
    assert "[Coding] Always use pnpm" in prompt


@pytest.mark.asyncio
async def test_master_handle_tool_call_memory_routing(tmp_path):
    bank = VeyaMemoryBank(storage_path=tmp_path / "m.json")
    coord = MasterCoordinator(memory_bank=bank)

    # 保存
    out = await coord.handle_tool_call(
        "system_save_preference", {"rule": "Keep replies under 50 words", "context": "Tone"}
    )
    assert "永久记忆已更新" in out
    assert bank.get_stats()["count"] == 1

    # 删除
    mem_id = bank.list_preferences()[0]["id"]
    out = await coord.handle_tool_call("system_remove_preference", {"memory_id": mem_id})
    assert "已擦除" in out
    assert bank.get_stats()["count"] == 0


@pytest.mark.asyncio
async def test_master_save_preference_via_chat_loop(tmp_path):
    """完整闭环: 模型调 save → 结果回喂 → 最终回答。"""
    bank = VeyaMemoryBank(storage_path=tmp_path / "m.json")
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            return _tool_response(
                "system_save_preference",
                {"rule": "My standard port is 8080", "context": "Coding"},
            )
        return _text_response("已记住: 标准端口 8080。")

    coord = MasterCoordinator(llm_fn=fake_llm, memory_bank=bank, max_rounds=3)
    result = await coord.chat_stream("我的标准端口是 8080", session_id="s1")
    assert result["status"] == "success"
    # 保存结果回喂
    assert "永久记忆已更新" in calls[1][-1]["content"]
    assert bank.search_preferences("8080")
