"""goal_run 执行侧分支(对标"Pi"清单 P2, 见 memory project_veya_pi_gap_audit 步骤8)。

之前验收失败重试是 `task.instruction` 原地字符串拼接, 旧尝试没有任何可查的
结构化记录。`_record_retry_branch` 把每次失败重试接到步骤2已落地的镜像
SessionTreeMgr(`default_session_tree_mirror()`, 跟 chat_stream 共用同一个
库/db)——失败节点 + 新叶分支, 用 `tree.path()` 能查到完整重试链路。

用内存 kv 隔离测试(同 tests/test_session_tree_mirror.py 的做法), 不碰真实
`~/.veya/sessions/session_tree_mirror.db`——那是跨进程共享的单例, 直接用会
在重复跑测试时累积陈旧数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from server.goal_run.runner import _record_retry_branch
from veya.obase.adapters import SqliteKvStore
from veya.omodul.session_tree import SessionTreeMgr


@dataclass
class _FakeTask:
    id: str
    title: str = "测试任务"
    instruction: str = "原始指令"
    retries: int = 0
    session_tree_sid: str | None = field(default=None)
    session_tree_leaf: str | None = field(default=None)


@pytest.fixture(autouse=True)
def _isolated_mirror(monkeypatch):
    """每个用例一棵全新内存树, 不跟真实单例/其它用例互相污染。"""
    mirror = SessionTreeMgr(kv=SqliteKvStore(":memory:"))
    monkeypatch.setattr("veya.omodul.session_tree.default_session_tree_mirror", lambda: mirror)
    return mirror


def test_record_retry_branch_creates_session_and_branches(_isolated_mirror):
    task = _FakeTask(id="t-branch-1")
    task.retries = 1

    _record_retry_branch(
        task,
        leaf_summary="第一次尝试失败: 测试没过",
        verify_reason="assertion failed",
        new_instruction="原始指令\n\n(上轮验收失败: assertion failed)",
    )

    assert task.session_tree_sid is not None
    assert task.session_tree_leaf is not None

    chain = _isolated_mirror.path(task.session_tree_sid)
    roles = [n["role"] for n in chain]
    contents = [n["content"] for n in chain]
    # root(system) → assistant(失败摘要) → user(新指令, 当前叶)
    assert roles == ["system", "assistant", "user"]
    assert "第一次尝试失败" in contents[1]
    assert contents[2] == "原始指令\n\n(上轮验收失败: assertion failed)"
    assert chain[-1]["id"] == task.session_tree_leaf


def test_record_retry_branch_second_retry_extends_same_tree_not_new_session(_isolated_mirror):
    task = _FakeTask(id="t-branch-2")
    task.retries = 1
    _record_retry_branch(
        task,
        leaf_summary="第一次失败",
        verify_reason="reason1",
        new_instruction="指令 v2",
    )
    sid_after_first = task.session_tree_sid

    task.retries = 2
    _record_retry_branch(
        task,
        leaf_summary="第二次失败",
        verify_reason="reason2",
        new_instruction="指令 v3",
    )

    assert task.session_tree_sid == sid_after_first  # 同一棵树, 不是新开会话

    chain = _isolated_mirror.path(task.session_tree_sid)
    # root → assistant(失败1) → user(v2) → assistant(失败2) → user(v3)
    assert len(chain) == 5
    assert chain[-1]["content"] == "指令 v3"
    # 失败1这个节点仍在树里可查(不是被覆盖丢失), 只是不在当前叶路径上多余保留
    assert any("第一次失败" in (n.get("content") or "") for n in chain)


def test_record_retry_branch_disabled_by_env_is_noop(monkeypatch, _isolated_mirror):
    monkeypatch.setenv("VEYA_GOAL_RUN_BRANCH_ENABLED", "0")
    task = _FakeTask(id="t-branch-disabled")

    _record_retry_branch(task, leaf_summary="失败", verify_reason="r", new_instruction="new")

    assert task.session_tree_sid is None
    assert task.session_tree_leaf is None


def test_record_retry_branch_swallows_session_tree_exception(monkeypatch):
    """镜像树本身异常(比如 kv 后端故障) 绝不能拖垮重试调度——静默跳过。"""

    def _boom():
        raise RuntimeError("kv backend down")

    task = _FakeTask(id="t-branch-error")
    # 直接 monkeypatch veya.omodul.session_tree.default_session_tree_mirror,
    # 因为 _record_retry_branch 用的是函数内 late import。
    monkeypatch.setattr("veya.omodul.session_tree.default_session_tree_mirror", _boom)

    # 不应抛异常
    _record_retry_branch(task, leaf_summary="失败", verify_reason="r", new_instruction="new")
    assert task.session_tree_sid is None
