"""Context Compaction: 纯函数窗口切分/渲染/合并 + MasterCoordinator 集成。

对标"Pi"长会话可靠性清单的 P0 三项之一(见 memory project_veya_pi_gap_audit):
取代 `master_agent._history_max_msgs=100` 硬截断(无摘要、不可逆)和
`_bound_llm._compact`(只裁剪发给 LLM 的临时视图, 不改持久历史) —— 本模块在
硬截断真正触发前更早介入, 原地改写 `_histories[sid]`, 摘要+保留尾部替换被
丢弃的中段, 并把每次压缩记成 decision_ledger 里可持久化的审计记录。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.decision_ledger import DecisionLedger
from veya.oskill.pure.context_compress import (
    _COMPACT_SUMMARY_PREFIX,
    build_compacted_messages,
    render_messages_for_summary,
    should_compact,
    split_compaction_window,
)


def _tool_call_group(call_id: str = "c1") -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": call_id, "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": "file contents"},
    ]


# ---------------------------------------------------------------------------
# should_compact
# ---------------------------------------------------------------------------


def test_should_compact_under_budget_is_false():
    msgs = [{"role": "user", "content": "短消息"}]
    assert should_compact(msgs, max_tokens=100000, trigger_ratio=0.7) is False


def test_should_compact_at_exact_threshold_is_true():
    # estimate_tokens: 英文约 4 字符/token；构造刚好达到 max_tokens*ratio 的内容
    msgs = [{"role": "user", "content": "a" * 400}]  # ~100 tokens
    assert should_compact(msgs, max_tokens=100, trigger_ratio=1.0) is True


def test_should_compact_degenerate_ratios():
    msgs = [{"role": "user", "content": "a" * 1000}]
    assert should_compact(msgs, max_tokens=0, trigger_ratio=0.7) is False
    assert should_compact(msgs, max_tokens=100, trigger_ratio=0) is False


# ---------------------------------------------------------------------------
# split_compaction_window
# ---------------------------------------------------------------------------


def test_split_never_splits_tool_call_group_when_tail_boundary_lands_inside():
    group = _tool_call_group("c1")
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "turn1"},
        *group,  # assistant(tool_calls) + tool  → 2 条, 原子不可拆
        {"role": "user", "content": "turn2"},
    ]
    # keep_tail_messages=1 恰好落在 group 内部（group 长度为 2）
    head, to_compact, tail = split_compaction_window(messages, keep_tail_messages=1)
    assert head == [messages[0]]
    # group 要么整体在 tail，要么整体在 to_compact —— 不允许拆开
    group_in_tail = group[0] in tail and group[1] in tail
    group_in_compact = group[0] in to_compact and group[1] in to_compact
    assert group_in_tail or group_in_compact
    assert not (group[0] in tail) ^ (group[1] in tail)  # 异或为假 = 同侧


def test_split_multiple_consecutive_tool_call_groups():
    g1, g2 = _tool_call_group("c1"), _tool_call_group("c2")
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        *g1,
        *g2,
        {"role": "user", "content": "u2"},
    ]
    head, to_compact, tail = split_compaction_window(messages, keep_tail_messages=3)
    # 每组各自完整地落在同一侧
    for g in (g1, g2):
        in_tail = g[0] in tail and g[1] in tail
        in_compact = g[0] in to_compact and g[1] in to_compact
        assert in_tail or in_compact


def test_split_orphan_tool_message_does_not_crash():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "ghost", "content": "orphan"},
        {"role": "user", "content": "u1"},
    ]
    head, to_compact, tail = split_compaction_window(messages, keep_tail_messages=1)
    assert head == [messages[0]]
    all_msgs = to_compact + tail
    assert messages[1] in all_msgs
    assert messages[2] in all_msgs


def test_split_degenerate_empty_body():
    messages = [{"role": "system", "content": "sys"}]
    head, to_compact, tail = split_compaction_window(messages, keep_tail_messages=10)
    assert head == messages
    assert to_compact == []
    assert tail == []


def test_split_degenerate_keep_tail_non_positive():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    head, to_compact, tail = split_compaction_window(messages, keep_tail_messages=0)
    assert tail == []
    assert to_compact == messages[1:]


def test_split_body_shorter_than_tail_target_keeps_all_in_tail():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    head, to_compact, tail = split_compaction_window(messages, keep_tail_messages=10)
    assert to_compact == []
    assert tail == messages[1:]


def test_split_single_unit_larger_than_tail_target():
    group = _tool_call_group("c1")
    messages = [{"role": "system", "content": "sys"}, *group]
    head, to_compact, tail = split_compaction_window(messages, keep_tail_messages=1)
    assert tail == group
    assert to_compact == []


def test_split_no_head_when_first_message_not_system():
    messages = [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}]
    head, to_compact, tail = split_compaction_window(messages, keep_tail_messages=1)
    assert head == []


# ---------------------------------------------------------------------------
# render_messages_for_summary
# ---------------------------------------------------------------------------


def test_render_includes_tool_call_name_and_args():
    messages = _tool_call_group("c1")
    text = render_messages_for_summary(messages)
    assert "read_file" in text
    assert "a.py" in text


def test_render_truncates_long_tool_result_without_crowding_others():
    messages = [
        {"role": "user", "content": "第一条正常消息"},
        {"role": "tool", "tool_call_id": "c1", "content": "X" * 5000},
        {"role": "user", "content": "第二条正常消息"},
    ]
    text = render_messages_for_summary(messages, max_chars=8000)
    assert "第一条正常消息" in text
    assert "第二条正常消息" in text
    assert text.count("X") <= 500


def test_render_empty_messages_returns_empty_string():
    assert render_messages_for_summary([]) == ""


# ---------------------------------------------------------------------------
# build_compacted_messages
# ---------------------------------------------------------------------------


def test_build_compacted_messages_structure_and_role():
    head = [{"role": "system", "content": "sys"}]
    tail = [{"role": "user", "content": "recent"}]
    out = build_compacted_messages(head, "一段摘要", tail)
    assert out[0] == head[0]
    assert out[-1] == tail[0]
    assert out[1]["role"] == "assistant"  # 绝不能是 system, 否则 _persist_history 会剥离
    assert out[1]["content"].startswith(_COMPACT_SUMMARY_PREFIX)
    assert "一段摘要" in out[1]["content"]


# ---------------------------------------------------------------------------
# MasterCoordinator 集成
# ---------------------------------------------------------------------------


@pytest.fixture
def coord():
    from server.coordinator_master import MasterCoordinator

    c = MasterCoordinator(max_rounds=1)
    return c


@pytest.fixture
def fake_ledger(tmp_path, monkeypatch):
    from server import decision_ledger as dl_mod

    ledger = DecisionLedger(db_path=tmp_path / "ledger.db")
    monkeypatch.setattr(dl_mod, "ledger", ledger)
    return ledger


def _big_history(n_pairs: int = 30) -> list[dict]:
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n_pairs):
        msgs.append({"role": "user", "content": f"用户第{i}轮问题 " + "内容" * 50})
        msgs.append({"role": "assistant", "content": f"助手第{i}轮回复 " + "内容" * 50})
    return msgs


@pytest.mark.asyncio
async def test_maybe_compact_mutates_histories_in_place(coord, fake_ledger, monkeypatch):
    monkeypatch.setenv("VEYA_CONTEXT_TOKEN_BUDGET", "200")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TRIGGER_RATIO", "0.5")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TAIL_MSGS", "4")
    sid = "s1"
    original = _big_history(30)
    coord._agent._histories = {sid: original}
    monkeypatch.setattr(
        coord,
        "_bound_llm",
        AsyncMock(return_value={"choices": [{"message": {"content": "压缩后的摘要文本"}}]}),
    )

    await coord._maybe_compact_history(sid)

    result = coord._agent._histories[sid]
    assert result is original  # 原地替换, 不是重新赋值
    assert len(result) < 60  # 明显短于原始 61 条
    assert result[0]["role"] == "system"
    assert any(
        m["role"] == "assistant" and m["content"].startswith(_COMPACT_SUMMARY_PREFIX)
        for m in result
    )
    rows = fake_ledger.export_ledger()
    assert any(r["category"] == "context_compaction" and r["outcome"] == "compacted" for r in rows)


@pytest.mark.asyncio
async def test_maybe_compact_noop_when_under_budget(coord, fake_ledger, monkeypatch):
    monkeypatch.setenv("VEYA_CONTEXT_TOKEN_BUDGET", "100000")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TRIGGER_RATIO", "0.7")
    sid = "s2"
    original = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    coord._agent._histories = {sid: list(original)}
    mock_llm = AsyncMock()
    monkeypatch.setattr(coord, "_bound_llm", mock_llm)

    await coord._maybe_compact_history(sid)

    assert coord._agent._histories[sid] == original
    mock_llm.assert_not_called()
    assert fake_ledger.export_ledger() == []


@pytest.mark.asyncio
async def test_maybe_compact_llm_failure_keeps_history_and_records_skip(
    coord, fake_ledger, monkeypatch
):
    monkeypatch.setenv("VEYA_CONTEXT_TOKEN_BUDGET", "200")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TRIGGER_RATIO", "0.5")
    sid = "s3"
    original = _big_history(30)
    original_copy = [dict(m) for m in original]
    coord._agent._histories = {sid: original}
    monkeypatch.setattr(coord, "_bound_llm", AsyncMock(side_effect=RuntimeError("网关抖动")))

    await coord._maybe_compact_history(sid)

    assert coord._agent._histories[sid] == original_copy
    rows = fake_ledger.export_ledger()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "skipped_summary_failed"
    assert "网关抖动" in rows[0]["reasoning"]


@pytest.mark.asyncio
async def test_maybe_compact_disabled_by_env(coord, fake_ledger, monkeypatch):
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_ENABLED", "0")
    monkeypatch.setenv("VEYA_CONTEXT_TOKEN_BUDGET", "200")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TRIGGER_RATIO", "0.5")
    sid = "s4"
    original = _big_history(30)
    coord._agent._histories = {sid: original}
    mock_llm = AsyncMock()
    monkeypatch.setattr(coord, "_bound_llm", mock_llm)

    await coord._maybe_compact_history(sid)

    assert coord._agent._histories[sid] == original
    mock_llm.assert_not_called()
    assert fake_ledger.export_ledger() == []


@pytest.mark.asyncio
async def test_maybe_compact_tail_preserves_tool_call_group(coord, fake_ledger, monkeypatch):
    monkeypatch.setenv("VEYA_CONTEXT_TOKEN_BUDGET", "150")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TRIGGER_RATIO", "0.5")
    monkeypatch.setenv("VEYA_CONTEXT_COMPACT_TAIL_MSGS", "1")  # 卡在组中间
    sid = "s5"
    original = _big_history(20)
    group = _tool_call_group("cX")
    original.extend(group)
    coord._agent._histories = {sid: original}
    monkeypatch.setattr(
        coord,
        "_bound_llm",
        AsyncMock(return_value={"choices": [{"message": {"content": "摘要"}}]}),
    )

    await coord._maybe_compact_history(sid)

    result = coord._agent._histories[sid]
    tool_msgs = [m for m in result if m.get("role") == "tool"]
    for tm in tool_msgs:
        call_id = tm.get("tool_call_id")
        # 每条 tool 结果都能在结果里找到对应的 assistant(tool_calls) 消息, 不是孤儿
        assert any(
            m.get("role") == "assistant"
            and any(tc.get("id") == call_id for tc in (m.get("tool_calls") or []))
            for m in result
        )
