"""runtime/state_authority/ownership — session projection writer ownership.

PR-07: make the implicit session_tree authority explicit and enforceable.

局面（审计结论，非理想化）:
- `veya/omodul/session_tree.py::SessionTreeMgr` 是唯一物理存储 (经 oprim.snapshot
  落 KvStore)。它持有**两类逻辑投影**, 但历史上有两个 Python 写入点:
  1. conversation projection —— `server/coordinator_master._mirror_to_session_tree`
     (把 history_store 的非 system 消息增量镜像进树, owner=会话 user_id)
  2. execution projection —— `server/goal_run/runner._record_retry_branch`
     (验收失败重试时 branch(), sid=`goalrun-<task.id>`, 与聊天 uuid sid 天然不撞)

两者写入**不同 session_id 命名空间** (前者 uuid4 聊天 sid, 后者 `goalrun-*`),
物理上不会覆盖同一 logical node; 但本 KvStore 层面没有"单 writer 约束", 未来
新增写入点很容易静默制造 authority ambiguity。

本模块不重写 SessionTreeMgr, 只定义:
- 两个逻辑 namespace 的枚举
- 每个 namespace 唯一 writer owner 的声明
- 一个轻量 register/assert 守卫, 供真实写入点在装配期/测试期调用

设计原则 (来自 PR-07 修订 Spec):
- 一个 logical projection namespace 只能有一个 authoritative projector/writer
  (这是对 SA-08 的精确化: 不是"整个 KvStore 只有一个 writer", 而是按 namespace)
- 调用者可以多个, 但 authoritative mutation implementation 只能一个
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class StateNamespace(StrEnum):
    """session_tree 内的逻辑投影命名空间。

    CONVERSATION: 聊天/主链对话的 session projection (来自 history_store 镜像)
    EXECUTION:    执行侧投影 (GoalRun 验收重试分支等)
    """

    CONVERSATION = "conversation"
    EXECUTION = "execution"


# 每个 namespace 的唯一 authoritative writer (实现位置)。
# 真实 mutator 是 SessionTreeMgr 的 append/branch, 但"谁有权调用它去写该 namespace"
# 由这里收敛: 只有声明过的 writer 才被允许。
_NAMESPACE_OWNER: Final[dict[StateNamespace, str]] = {
    StateNamespace.CONVERSATION: "SessionProjector",  # coordinator_master._mirror_to_session_tree
    StateNamespace.EXECUTION: "GoalRunProjection",  # goal_run/runner._record_retry_branch
}


@dataclass(frozen=True)
class StateWriterOwnership:
    """声明: 某 namespace 的唯一 writer owner。

    用于静态/单测 guard + 文档化 contract。不含运行期强制 (KvStore 不感知
    namespace), 强制靠 PHASE 9 的静态 guard + 本模块提供的断言辅助。
    """

    namespace: StateNamespace
    writer: str


def owner_of(namespace: StateNamespace) -> str:
    """返回该 namespace 的 authoritative writer 名。"""
    return _NAMESPACE_OWNER[namespace]


def assert_writer(namespace: StateNamespace, candidate: str) -> None:
    """守卫: candidate 必须是该 namespace 声明的唯一 writer。

    非 owner 调用_session_tree 写该 namespace 时, 在装配/测试期抛 AssertionError。
    运行期调用点 (coordinator / goal_run) 应在初始化时各自断言自己是谁, 而不是
    在每条 append 上检查 (避免热路径开销与误用混入)。
    """
    expected = _NAMESPACE_OWNER[namespace]
    if candidate != expected:
        raise AssertionError(
            f"namespace {namespace.value!r} writer must be {expected!r}, got {candidate!r}"
        )


def assert_session_id(namespace: StateNamespace, sid: str) -> None:
    """Reject a session id that belongs to the other logical namespace."""
    is_execution = str(sid).startswith("goalrun-")
    if namespace is StateNamespace.EXECUTION and not is_execution:
        raise AssertionError("execution namespace requires a goalrun-* session id")
    if namespace is StateNamespace.CONVERSATION and is_execution:
        raise AssertionError("conversation namespace cannot use a goalrun-* session id")


def declared_ownership() -> list[StateWriterOwnership]:
    """返回当前全部 namespace 的 owner 声明 (state doctor / 文档用)。"""
    return [StateWriterOwnership(ns, owner) for ns, owner in _NAMESPACE_OWNER.items()]
