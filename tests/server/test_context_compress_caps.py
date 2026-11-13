"""上下文压缩两道独立上限 (orkas-4)。

目标: 证明“单条工具结果准入上限”与“压缩输入总量上限”互不干扰, 且
VEYA_COMPACT_SUMMARY_MAX_CHARS 真正生效 (此前被 render_messages_for_summary 的
8000 默认值提前截断/覆盖)。
"""

from __future__ import annotations

import pytest

from server.coordinator_master import MasterCoordinator
from veya.oskill.pure.context_compress import render_messages_for_summary


def _tool_msgs(pad: int = 200) -> list[dict]:
    """一段超长对话 + 一条超长工具结果, 渲染后总量远大于 40000, 便于观察上限生效。"""
    msgs: list[dict] = [{"role": "system", "content": "sys"}]
    for _i in range(pad):
        msgs.append({"role": "user", "content": "用户消息内容填充 " * 20})
        msgs.append({"role": "assistant", "content": "助手回复内容填充 " * 20})
    msgs.append({"role": "user", "content": "最后一轮用户诉求 " * 20})
    msgs.append({"role": "tool", "tool_call_id": "c1", "content": "X" * 5000})
    return msgs


# ── render_messages_for_summary: 总量上限与单条截断互不干扰 ──────────────────


def test_render_total_cap_is_independent_of_snippet_cap():
    msgs = _tool_msgs()
    # 默认 8000 总量 + 500 单条。
    out_default = render_messages_for_summary(msgs)
    # 把总量上限拉大到远超输入, 单条仍应被截到 500。
    out_big_total = render_messages_for_summary(msgs, max_chars=1_000_000)
    assert len(out_default) <= 8000
    assert len(out_big_total) > 8000  # 总量上限生效, 输入确实被完整保留
    # 单条工具结果始终被截到 500, 两种总量下都应是 "X"*500 + 不再有后续 X。
    for out in (out_default, out_big_total):
        assert "X" * 500 in out
        # 单条截断后不应出现接近原长 (5000) 的连续 X 块。
        assert "X" * 600 not in out


def test_render_snippet_cap_independent_of_total(monkeypatch):
    msgs = _tool_msgs()
    out = render_messages_for_summary(msgs, max_chars=1_000_000, tool_result_snippet_chars=50)
    # 单条被截到 50。
    assert "X" * 50 in out
    assert "X" * 51 not in out
    # 总量上限拉满, 不影响单条 50 的截断。
    assert len(out) > 8000


def test_render_empty_messages_returns_empty_string():
    assert render_messages_for_summary([]) == ""


# ── _summarize_for_compaction: 总量上限真正生效 (不再被 8000 覆盖) ───────────


class _StubLLM:
    """记录请求中对话片段的长度, 并返回占位摘要。"""

    def __init__(self) -> None:
        self.last_convo_len = 0

    async def __call__(self, messages: list[dict]) -> dict:
        user = messages[-1]
        # _summarize_for_compaction 把渲染后的对话放在 user content 的
        # 「对话片段:\n{convo}\n\n请输出摘要。」里, 精确取出 convo 长度。
        content = user.get("content") or ""
        marker_head = "对话片段:\n"
        marker_tail = "\n\n请输出摘要。"
        if content.startswith(marker_head) and content.endswith(marker_tail):
            inner = content[len(marker_head) : -len(marker_tail)]
        else:
            inner = content
        self.last_convo_len = len(inner)
        return {"choices": [{"message": {"content": "## Summary\nok\n\n## Exact facts\n"}}]}


def _coordinator() -> MasterCoordinator:
    class _HS:
        async def load(self, *_a, **_k):
            return []

        async def save(self, *_a, **_k):
            return None

    class _MS:
        async def retrieve(self, *_a, **_k):
            return []

        async def add(self, *_a, **_k):
            return ""

    return MasterCoordinator(history_store=_HS(), memory_store=_MS())


@pytest.mark.asyncio
async def test_summary_input_grows_with_bigger_compact_summary_max_chars(monkeypatch):
    """调大 VEYA_COMPACT_SUMMARY_MAX_CHARS 后, 摘要输入的对话片段确实变长。"""
    msgs = _tool_msgs()
    coord = _coordinator()
    stub = _StubLLM()
    coord._compact_llm_fn = stub

    monkeypatch.setenv("VEYA_COMPACT_SUMMARY_MAX_CHARS", "8000")
    await coord._summarize_for_compaction(msgs)
    len_8000 = stub.last_convo_len

    monkeypatch.setenv("VEYA_COMPACT_SUMMARY_MAX_CHARS", "40000")
    await coord._summarize_for_compaction(msgs)
    len_40000 = stub.last_convo_len

    # 拉大上限后摘要输入确实变长 (原来 8000 默认值会把 40000 覆盖掉)。
    assert len_40000 > len_8000 + 1000
    assert len_8000 <= 8000
    assert len_40000 > 8000


@pytest.mark.asyncio
async def test_summary_input_snippet_cap_independent_of_total(monkeypatch):
    """压缩输入的单条工具结果截断长度可单独调节, 不影响总量上限。

    把总量上限拉到远超输入, 这样总量上限不截断, 单条工具结果截断 (100 vs 500)
    成为唯一变量: 放宽后输入变长, 但仍不会把 5000 长的原工具结果整段放进来。
    """
    msgs = _tool_msgs()
    coord = _coordinator()
    stub = _StubLLM()
    coord._compact_llm_fn = stub

    monkeypatch.setenv("VEYA_COMPACT_SUMMARY_MAX_CHARS", "400000")
    monkeypatch.setenv("VEYA_COMPACT_TOOL_RESULT_SNIPPET_CHARS", "100")
    await coord._summarize_for_compaction(msgs)
    convo100 = stub.last_convo_len

    monkeypatch.setenv("VEYA_COMPACT_TOOL_RESULT_SNIPPET_CHARS", "500")
    await coord._summarize_for_compaction(msgs)
    convo500 = stub.last_convo_len

    assert convo500 > convo100  # 单条截断放宽 → 输入变长, 证明不是 8000/总量在主导
    assert convo500 > 8000  # 总量上限 (40 万) 确实把 8000 默认值覆盖掉了
    assert "X" * 5000 not in render_messages_for_summary(
        msgs, max_chars=400000, tool_result_snippet_chars=500
    )  # 单条仍被 500 截断, 不把原长整段放进来


# ── _admit_tool_result_capped: 准入上限只作用于副本, 不改持久化历史 ──────────


def test_admit_tool_result_caps_copy_only_and_is_independent():
    coord = _coordinator()
    persisted = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "c1", "content": "Y" * 5000},
    ]
    source = [dict(m) for m in persisted]

    capped = coord._admit_tool_result_capped(source)
    # 不改持久化历史副本。
    assert persisted[1]["content"] == "Y" * 5000
    assert source[1]["content"] == "Y" * 5000
    # 返回的副本里工具结果被截到默认 40000 (此处 5000 < 40000, 应原样)。
    assert capped[1]["content"] == "Y" * 5000
    assert capped is source  # 无截断时复用同一列表, 零拷贝


def test_admit_tool_result_actually_caps_oversized(monkeypatch):
    coord = _coordinator()
    msgs = [
        {"role": "tool", "tool_call_id": "c1", "content": "Z" * 12345},
        {"role": "user", "content": "keep"},
    ]
    monkeypatch.setenv("VEYA_CONTEXT_TOOL_RESULT_ADMIT_MAX_CHARS", "100")
    capped = coord._admit_tool_result_capped(msgs)
    assert capped[0]["content"] == "Z" * 100 + "…"
    assert capped[0] is not msgs[0]  # 是新副本
    assert msgs[0]["content"] == "Z" * 12345  # 原消息未改
    assert capped[1]["content"] == "keep"


def test_admit_tool_result_disabled_keeps_history(monkeypatch):
    coord = _coordinator()
    msgs = [{"role": "tool", "tool_call_id": "c1", "content": "Z" * 12345}]
    monkeypatch.setenv("VEYA_CONTEXT_TOOL_RESULT_ADMIT_MAX_CHARS", "0")
    capped = coord._admit_tool_result_capped(msgs)
    assert capped is msgs
    assert capped[0]["content"] == "Z" * 12345
