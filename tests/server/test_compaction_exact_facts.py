"""Exact-facts 压缩增强：合并 / 兜底 / 超大 tool 截断 / 开关关闭一致。

纯函数部分在 ``tests/test_context_compaction.py``；本文件覆盖
``MasterCoordinator._maybe_compact_history`` 的端到端行为。
"""

from __future__ import annotations

import pytest

from server.coordinator_master import MasterCoordinator
from veya.oskill.pure.context_compress import _COMPACT_SUMMARY_PREFIX


class _HistoryStore:
    async def load(self, *_a, **_k):
        return []

    async def save(self, *_a, **_k):
        return None


class _MemoryStore:
    async def retrieve(self, *_a, **_k):
        return []

    async def add(self, *_a, **_k):
        return ""


def _coordinator(**kwargs):
    return MasterCoordinator(history_store=_HistoryStore(), memory_store=_MemoryStore(), **kwargs)


def _summary(content: str) -> dict:
    return {"role": "assistant", "content": f"{_COMPACT_SUMMARY_PREFIX}\n{content}"}


def _big_history(turns: int = 30) -> list[dict]:
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(turns):
        msgs.append({"role": "user", "content": f"用户第{i}轮 " + "内容" * 50})
        msgs.append({"role": "assistant", "content": f"助手第{i}轮 " + "内容" * 50})
    return msgs


@pytest.fixture
def fake_ledger(tmp_path, monkeypatch):
    from server import decision_ledger as dl_mod

    ledger = dl_mod.DecisionLedger(db_path=tmp_path / "ledger.db")
    monkeypatch.setattr(dl_mod, "ledger", ledger)
    return ledger


# ---------------------------------------------------------------------------
# Exact facts 合并进新摘要
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_facts_merged_from_prior_summary(fake_ledger, monkeypatch):
    monkeypatch.setenv("VEYA_CONTEXT_TOKEN_BUDGET", "200")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TRIGGER_RATIO", "0.5")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TAIL_MSGS", "4")
    sid = "s-ef-1"
    original = [
        *[m for m in _big_history(30)[:-4]],
        # 上一份压缩摘要, 含 Exact facts
        _summary(
            "## Summary\n之前已读 config.\n\n## Exact facts\norder_id=ORD-123\npath=/repo/a.py\n"
        ),
        {"role": "user", "content": "继续 " + "内容" * 50},
        {"role": "assistant", "content": "好 " + "内容" * 50},
    ]
    coord = _coordinator()
    coord._agent._histories = {sid: original}

    captured = {}

    async def _summarize(messages):
        captured["n"] = len(messages)
        # 新摘要: retry 覆盖 order 的... 这里覆盖 path, 新增 decision
        return "## Summary\n现在改了 b.py.\n\n## Exact facts\npath=/repo/b.py\ndecision=use_x\n"

    coord._summarize_for_compaction = _summarize
    await coord._maybe_compact_history(sid)

    result = coord._agent._histories[sid]
    summary = next(m for m in result if m.get("content", "").startswith(_COMPACT_SUMMARY_PREFIX))
    body = summary["content"][len(_COMPACT_SUMMARY_PREFIX) :]
    assert "order_id=ORD-123" in body  # prev 保留
    assert "path=/repo/b.py" in body  # new 覆盖
    assert "path=/repo/a.py" not in body
    assert "decision=use_x" in body  # new 新增


# ---------------------------------------------------------------------------
# 兜底: 新摘要缺 Exact facts 节 → 保留上一份原文
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_retains_prior_when_new_missing_section(fake_ledger, monkeypatch):
    monkeypatch.setenv("VEYA_CONTEXT_TOKEN_BUDGET", "200")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TRIGGER_RATIO", "0.5")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TAIL_MSGS", "4")
    sid = "s-ef-2"
    original = [
        *[m for m in _big_history(30)[:-4]],
        _summary("## Summary\n之前摘要.\n\n## Exact facts\nimportant_id=ID-9\n"),
        {"role": "user", "content": "继续 " + "内容" * 50},
        {"role": "assistant", "content": "好 " + "内容" * 50},
    ]
    coord = _coordinator()
    coord._agent._histories = {sid: original}

    async def _summarize(_messages):
        # 模型没按固定小节输出 → 缺 Exact facts
        return "只有一段自由文本摘要, 没有固定小节。"

    coord._summarize_for_compaction = _summarize
    await coord._maybe_compact_history(sid)

    result = coord._agent._histories[sid]
    summary = next(m for m in result if m.get("content", "").startswith(_COMPACT_SUMMARY_PREFIX))
    body = summary["content"][len(_COMPACT_SUMMARY_PREFIX) :]
    assert "Prior summary retained verbatim" in body
    assert "important_id=ID-9" in body  # 上一份事实未丢


# ---------------------------------------------------------------------------
# 超大 tool 结果截断 (仅副本, 不改持久化历史)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_tool_result_truncated_in_copy_only(fake_ledger, monkeypatch):
    monkeypatch.setenv("VEYA_CONTEXT_TOKEN_BUDGET", "200")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TRIGGER_RATIO", "0.5")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TAIL_MSGS", "4")
    monkeypatch.setenv("VEYA_CONTEXT_TOOL_RESULT_MAX_CHARS", "400")
    sid = "s-ef-3"
    original = _big_history(30)
    call_id = "big1"
    original.insert(
        1,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "function": {"name": "run", "arguments": "{}"}}],
        },
    )
    original.insert(2, {"role": "tool", "tool_call_id": call_id, "content": "Z" * 10000})
    tool_msg = original[2]
    coord = _coordinator()
    coord._agent._histories = {sid: original}

    seen = {}

    async def _summarize(messages):
        seen["messages"] = messages
        return "## Summary\nok\n\n## Exact facts\n"

    coord._summarize_for_compaction = _summarize
    await coord._maybe_compact_history(sid)

    # 持久化历史中的原始 tool 消息未被改动
    assert tool_msg["content"] == "Z" * 10000
    # 送给摘要的副本被截断
    sent = next(m for m in seen["messages"] if m.get("tool_call_id") == call_id)
    assert "[truncated" in sent["content"]
    assert len(sent["content"]) < 10000
    # tool_call 配对仍完整: assistant 消息还在副本里
    assert any(
        m.get("role") == "assistant"
        and any(tc.get("id") == call_id for tc in (m.get("tool_calls") or []))
        for m in seen["messages"]
    )


# ---------------------------------------------------------------------------
# 开关关闭 → 行为与原来一致 (无 Exact facts 合并, 无 tool 截断)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_keeps_legacy_behavior(fake_ledger, monkeypatch):
    monkeypatch.setenv("VEYA_CONTEXT_EXACT_FACTS", "0")
    monkeypatch.setenv("VEYA_CONTEXT_TOKEN_BUDGET", "200")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TRIGGER_RATIO", "0.5")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TAIL_MSGS", "4")
    monkeypatch.setenv("VEYA_CONTEXT_TOOL_RESULT_MAX_CHARS", "400")
    sid = "s-ef-4"
    original = _big_history(30)
    call_id = "big2"
    original.insert(
        1,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "function": {"name": "run", "arguments": "{}"}}],
        },
    )
    original.insert(2, {"role": "tool", "tool_call_id": call_id, "content": "W" * 10000})
    coord = _coordinator()
    coord._agent._histories = {sid: original}

    seen = {}

    async def _summarize(messages):
        seen["messages"] = messages
        return "## Summary\nok\n\n## Exact facts\n"

    coord._summarize_for_compaction = _summarize
    await coord._maybe_compact_history(sid)

    # tool 结果未被截断 (开关关闭)
    sent = next(m for m in seen["messages"] if m.get("tool_call_id") == call_id)
    assert sent["content"] == "W" * 10000
    # 新摘要原样落回, 未做 Exact facts 合并
    result = coord._agent._histories[sid]
    summary = next(m for m in result if m.get("content", "").startswith(_COMPACT_SUMMARY_PREFIX))
    body = summary["content"][len(_COMPACT_SUMMARY_PREFIX) :]
    assert "## Exact facts" in body  # 原样, 与我返回的一致
