"""Resume 幂等 (对标"Pi"清单 P1 Harness 生命周期)——中断后恢复不重复已落盘副作用。

进程若在一批 tool_calls 执行完成/结果落盘前崩溃, 冷启动恢复出的历史尾部会是一条
悬空的 assistant tool_calls (没有对应 tool 结果)。`_repair_dangling_tool_calls`
把这种协议不完整的历史补成合法的, 并明确标记"结果未知, 别不假思索地重试"——
而不是让模型看到"没结果"就误判成"还没跑过"从而把同一个有副作用的调用再发一遍。
"""

from __future__ import annotations

import pytest

from server.coordinator_master import (
    _INTERRUPTED_TOOL_NOTICE,
    MasterCoordinator,
    _repair_dangling_tool_calls,
)


def test_empty_list_unchanged():
    assert _repair_dangling_tool_calls([]) == []


@pytest.mark.parametrize(
    "last",
    [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "done, no tools"},
        {"role": "assistant", "content": "done", "tool_calls": []},
    ],
)
def test_non_dangling_tail_unchanged(last):
    messages = [{"role": "user", "content": "go"}, last]
    assert _repair_dangling_tool_calls(messages) == messages


def test_single_dangling_tool_call_gets_placeholder():
    messages = [
        {"role": "user", "content": "write a file"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": "{}"},
                }
            ],
        },
    ]
    out = _repair_dangling_tool_calls(messages)
    assert len(out) == 3
    assert out[:2] == messages  # 原消息不变
    assert out[2] == {"role": "tool", "tool_call_id": "call_1", "content": _INTERRUPTED_TOOL_NOTICE}


def test_parallel_batch_gets_one_placeholder_per_call_in_order():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                },
                {
                    "id": "call_b",
                    "type": "function",
                    "function": {"name": "write", "arguments": "{}"},
                },
                {
                    "id": "call_c",
                    "type": "function",
                    "function": {"name": "send", "arguments": "{}"},
                },
            ],
        }
    ]
    out = _repair_dangling_tool_calls(messages)
    assert len(out) == 4
    assert [m["tool_call_id"] for m in out[1:]] == ["call_a", "call_b", "call_c"]
    assert all(m["role"] == "tool" and m["content"] == _INTERRUPTED_TOOL_NOTICE for m in out[1:])


def test_malformed_tool_call_missing_id_is_skipped_not_crashed():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"type": "function", "function": {"name": "x", "arguments": "{}"}},  # 缺 id
                {"id": "call_ok", "type": "function", "function": {"name": "y", "arguments": "{}"}},
            ],
        }
    ]
    out = _repair_dangling_tool_calls(messages)
    assert len(out) == 2
    assert out[1]["tool_call_id"] == "call_ok"


def test_does_not_mutate_input_in_place():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
            ],
        }
    ]
    original_len = len(messages)
    _repair_dangling_tool_calls(messages)
    assert len(messages) == original_len  # 原列表未被原地追加


class _FakeStore:
    def __init__(self, blob: list) -> None:
        self._blob = blob

    async def load(self, sid: str) -> list:
        return list(self._blob)

    async def save(self, sid: str, msgs: list) -> None:
        self._blob = list(msgs)


@pytest.mark.asyncio
async def test_restore_history_repairs_dangling_tail():
    dangling = [
        {"role": "user", "content": "write a file"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": "{}"},
                }
            ],
        },
    ]
    coord = MasterCoordinator(max_rounds=1, history_store=_FakeStore(dangling))
    await coord._restore_history("s1")
    restored = coord._agent._histories["s1"]
    assert restored[0]["role"] == "system"
    assert restored[1:3] == dangling
    assert restored[3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": _INTERRUPTED_TOOL_NOTICE,
    }


@pytest.mark.asyncio
async def test_restore_history_clean_completion_unchanged():
    clean = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "done, all good"},
    ]
    coord = MasterCoordinator(max_rounds=1, history_store=_FakeStore(clean))
    await coord._restore_history("s2")
    restored = coord._agent._histories["s2"]
    assert restored[0]["role"] == "system"
    assert restored[1:] == clean
