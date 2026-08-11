"""主脑长任务无损恢复: 运行期定时快照 + 冷启动恢复 (server.coordinator_master)。

主库 ReAct 循环在 _histories[sid] 原地累积每轮消息; MasterCoordinator 在 chat_stream
运行期间起 _checkpoint_loop 定时落盘, 使进程崩溃后 _restore_history 能续跑。
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from server.coordinator_master import MasterCoordinator


class _FakeStore:
    def __init__(self) -> None:
        self.saved: list[tuple[str, list]] = []
        self._blob: list = []

    async def save(self, sid: str, msgs: list) -> None:
        self.saved.append((sid, list(msgs)))
        self._blob = list(msgs)

    async def load(self, sid: str) -> list:
        return list(self._blob)


@pytest.fixture
def coord():
    c = MasterCoordinator(max_rounds=1)
    c._history_store = _FakeStore()
    return c


@pytest.mark.asyncio
async def test_checkpoint_loop_persists_periodically(coord, monkeypatch):
    monkeypatch.setenv("VEYA_CHECKPOINT_INTERVAL_S", "0.01")
    coord._agent._histories = {
        "s1": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "查一下比特币"},
            {"role": "assistant", "content": "查询中"},
        ]
    }
    task = asyncio.create_task(coord._checkpoint_loop("s1"))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    store = coord._history_store
    assert store.saved, "长任务期间应至少落一次快照"
    sid, msgs = store.saved[-1]
    assert sid == "s1"
    # system 消息不入持久化 (恢复时用当前版本 system 重建)
    assert msgs == [
        {"role": "user", "content": "查一下比特币"},
        {"role": "assistant", "content": "查询中"},
    ]


@pytest.mark.asyncio
async def test_checkpoint_loop_disabled_by_zero_interval(coord, monkeypatch):
    monkeypatch.setenv("VEYA_CHECKPOINT_INTERVAL_S", "0")
    coord._agent._histories = {"s1": [{"role": "user", "content": "hi"}]}
    task = asyncio.create_task(coord._checkpoint_loop("s1"))
    await asyncio.sleep(0.03)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert coord._history_store.saved == []  # 关闭 → 零落盘


@pytest.mark.asyncio
async def test_snapshot_then_cold_restore_roundtrip(coord):
    # 模拟中途快照
    coord._agent._histories = {
        "s2": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "step1"},
            {"role": "assistant", "content": "partial"},
        ]
    }
    await coord._persist_history("s2")
    # 模拟进程重启: 清空热缓存 → 冷启动恢复
    coord._agent._histories.clear()
    await coord._restore_history("s2")
    restored = coord._agent._histories["s2"]
    assert restored[0]["role"] == "system"  # 用当前版本 system 重建
    assert restored[1:] == [
        {"role": "user", "content": "step1"},
        {"role": "assistant", "content": "partial"},
    ]
