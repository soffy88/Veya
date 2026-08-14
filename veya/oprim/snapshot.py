"""veya/oprim/snapshot — Session Tree 快照原子操作（物理触手）。

阶段 3 原子元素：oprim_db_commit_snapshot / oprim_db_fetch_snapshot /
oprim_db_list_snapshots / oprim_db_delete_snapshot。

规则：
- 所有存取经注入的 KvStore 句柄（默认 container 全局句柄）；
- 键空间前缀 ``session_tree:`` 由本层统一管理（阶段 4 session_tree_mgr 使用）；
- 原子性：commit = 单键写入，fetch = 单键读取，值必须是 JSON 可序列化。
"""

from __future__ import annotations

from typing import Any

_KEY_PREFIX = "session_tree:"


def _kv_of(kv: Any) -> Any:
    if kv is not None:
        return kv
    from veya.obase.container import get_kv

    return get_kv()


def snapshot_commit(tree_id: str, data: Any, kv: Any = None) -> None:
    """提交会话树快照（原子单键写入）。"""
    _kv_of(kv).put(f"{_KEY_PREFIX}{tree_id}", data)  # type: ignore[attr-defined]


def snapshot_fetch(tree_id: str, kv: Any = None) -> Any:
    """取回会话树快照（不存在返回 None）。"""
    return _kv_of(kv).get(f"{_KEY_PREFIX}{tree_id}")  # type: ignore[attr-defined]


def snapshot_list(kv: Any = None) -> list[str]:
    """列出全部已提交快照的 tree_id（剥离前缀）。"""
    keys = _kv_of(kv).keys(_KEY_PREFIX)  # type: ignore[attr-defined]
    return [k[len(_KEY_PREFIX) :] for k in keys]


def snapshot_delete(tree_id: str, kv: Any = None) -> None:
    """删除会话树快照。"""
    _kv_of(kv).delete(f"{_KEY_PREFIX}{tree_id}")  # type: ignore[attr-defined]


__all__ = ["snapshot_commit", "snapshot_delete", "snapshot_fetch", "snapshot_list"]
