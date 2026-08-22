"""SessionTreeMgr 镜像写入主链历史 (P0 对标"Pi"清单步骤2)——只写不读的旁路镜像。

权威源仍是 SqliteHistoryStore (docs/ARCHITECTURE_STABLE.md 冻结决定), 这里只验证
镜像树本身的正确性/隔离性: 纯追加不重复、头部重写(Compaction)时旧节点不被删除、
关闭开关时零调用、镜像失败不拖垮权威落盘、tool_call_id 往返不丢。
"""

from __future__ import annotations

import pytest

from server.coordinator_master import MasterCoordinator
from veya.obase.adapters import SqliteKvStore
from veya.omodul.session_tree import SessionTreeMgr


class _FakeStore:
    def __init__(self) -> None:
        self.saved: list[tuple[str, list]] = []

    async def save(self, sid: str, msgs: list) -> None:
        self.saved.append((sid, list(msgs)))

    async def load(self, sid: str) -> list:
        return []


@pytest.fixture
def tree() -> SessionTreeMgr:
    return SessionTreeMgr(kv=SqliteKvStore(":memory:"))


@pytest.fixture
def coord(tree):
    return MasterCoordinator(max_rounds=1, history_store=_FakeStore(), session_tree=tree)


def _nodes(tree: SessionTreeMgr, sid: str) -> dict:
    snap = tree.snapshot(sid)
    return snap["tree"]["nodes"] if snap else {}


@pytest.mark.asyncio
async def test_first_persist_builds_linear_tree(coord, tree):
    coord._agent._histories = {
        "s1": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
    }
    await coord._persist_history("s1")
    # tree.messages() 的第0条是树自己的 root system 节点 (ensure_session 建的空占位,
    # 不是主链的真实 system prompt) —— 镜像只关心非 system 部分, 跳过它比对。
    assert tree.messages("s1")[1:] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


@pytest.mark.asyncio
async def test_second_persist_appends_only_new_tail(coord, tree):
    hist = {
        "s1": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
    }
    coord._agent._histories = hist
    await coord._persist_history("s1")
    before_ids = set(_nodes(tree, "s1"))

    hist["s1"].append({"role": "user", "content": "second turn"})
    hist["s1"].append({"role": "assistant", "content": "second reply"})
    await coord._persist_history("s1")

    after_ids = set(_nodes(tree, "s1"))
    assert before_ids <= after_ids  # 旧节点原样保留, 没被重建
    assert len(after_ids) == len(before_ids) + 2  # 只新增了2条, 没有重复
    assert tree.messages("s1")[1:] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "second turn"},
        {"role": "assistant", "content": "second reply"},
    ]


@pytest.mark.asyncio
async def test_head_rewrite_branches_and_keeps_old_nodes(coord, tree):
    """模拟 Compaction: msgs[:] 用摘要替换头部, 尾部内容原样保留。"""
    hist = {
        "s1": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "turn1"},
            {"role": "assistant", "content": "reply1"},
            {"role": "user", "content": "turn2"},
            {"role": "assistant", "content": "reply2"},
        ]
    }
    coord._agent._histories = hist
    await coord._persist_history("s1")
    old_ids = set(_nodes(tree, "s1"))

    hist["s1"][:] = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "[摘要] turn1/reply1 已压缩"},
        {"role": "user", "content": "turn2"},
        {"role": "assistant", "content": "reply2"},
    ]
    await coord._persist_history("s1")

    new_nodes = _nodes(tree, "s1")
    assert old_ids <= set(new_nodes)  # 分支不删除历史
    assert tree.messages("s1")[1:] == [
        {"role": "assistant", "content": "[摘要] turn1/reply1 已压缩"},
        {"role": "user", "content": "turn2"},
        {"role": "assistant", "content": "reply2"},
    ]


@pytest.mark.asyncio
async def test_disabled_env_var_skips_mirror(coord, tree):
    coord._session_tree_mirror_enabled = False
    coord._agent._histories = {
        "s1": [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    }
    await coord._persist_history("s1")
    assert tree.snapshot("s1") is None  # 从未创建过会话树


@pytest.mark.asyncio
async def test_mirror_failure_does_not_break_persist(coord):
    class _BoomTree:
        def ensure_session(self, *a, **k):
            raise RuntimeError("boom")

    coord._session_tree = _BoomTree()
    coord._agent._histories = {
        "s1": [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    }
    await coord._persist_history("s1")  # 不应抛出
    assert coord._history_store.saved  # 权威落盘仍然成功


@pytest.mark.asyncio
async def test_tool_message_round_trips_tool_call_id(coord, tree):
    coord._agent._histories = {
        "s1": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "run tool"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "foo", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "tool result"},
        ]
    }
    await coord._persist_history("s1")
    msgs = tree.messages("s1")
    assert msgs[-1] == {"role": "tool", "tool_call_id": "call_1", "content": "tool result"}
    assert msgs[-2]["tool_calls"] == [
        {"id": "call_1", "type": "function", "function": {"name": "foo", "arguments": "{}"}}
    ]
