"""veya/omodul/session_tree — omodul_session_tree_mgr（会话树管理）。

阶段 4 核心元素：真正支持 branching / 时空回溯的会话树。

结构（参考 Pi 的 id/parentId + leaf 指针）:
    session = {"root": node_id, "leaf": node_id, "nodes": {id: node}}
    node    = {"id", "parent_id", "role", "content", "tool_calls", "meta"}

注入:
    kv    — KvStore 句柄（默认 container.get_kv），经 oprim.snapshot 键空间持久化
    id_fn — 节点 id 生成器（默认 uuid4；测试注入确定性生成器）

能力:
    create_session / append / branch / leaf / path / fork / snapshot / restore
    path()  = 根→叶消息链（时空回溯 / LLM 上下文重建）
    fork()  = 从任意节点复制出全新会话（分支探索）
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from veya.oprim.snapshot import (
    snapshot_commit,
    snapshot_delete,
    snapshot_fetch,
)

_ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})


def _default_id() -> str:
    return uuid.uuid4().hex[:12]


class SessionTreeMgr:
    """会话树管理器：节点级 DAG（每节点单父 → 树），leaf 指针标记当前轨迹。"""

    def __init__(self, kv: Any = None, *, id_fn: Callable[[], str] = _default_id) -> None:
        if kv is None:
            from veya.obase.container import get_kv

            kv = get_kv()
        self._kv = kv
        self._id_fn = id_fn

    # ------------------------------------------------------------------ 树操作

    def create_session(self, *, system: str | None = None) -> str:
        """创建新会话（根节点 = system 提示，可省略）。返回 session_id。"""
        sid = self._id_fn()
        root_id = self._id_fn()
        nodes: dict[str, dict[str, Any]] = {}
        if system is not None:
            nodes[root_id] = self._node(root_id, None, "system", system)
        else:
            nodes[root_id] = self._node(root_id, None, "system", "")
        self._save(sid, {"root": root_id, "leaf": root_id, "nodes": nodes})
        return sid

    def ensure_session(self, sid: str, *, system: str | None = None) -> str:
        """确保会话存在：不存在则用指定 sid 创建（外部传入 session_id 兼容）。
        已存在则原样返回。"""
        if self._load(sid) is None:
            root_id = self._id_fn()
            nodes = {root_id: self._node(root_id, None, "system", system or "")}
            self._save(sid, {"root": root_id, "leaf": root_id, "nodes": nodes})
        return sid

    def append(
        self,
        sid: str,
        *,
        role: str,
        content: Any,
        parent_id: str | None = None,
        tool_calls: list | None = None,
        meta: dict[str, Any] | None = None,
    ) -> str:
        """在（默认当前叶）下追加消息节点。返回新节点 id。"""
        if role not in _ROLES:
            raise ValueError(f"非法 role {role!r} (允许: {sorted(_ROLES)})")
        tree = self._load(sid)
        parent = parent_id or tree["leaf"]
        if parent not in tree["nodes"]:
            raise ValueError(f"父节点 {parent!r} 不存在于会话 {sid!r}")
        node_id = self._id_fn()
        tree["nodes"][node_id] = self._node(
            node_id, parent, role, content, tool_calls=tool_calls, meta=meta
        )
        tree["leaf"] = node_id
        self._save(sid, tree)
        return node_id

    def branch(self, sid: str, *, at_node_id: str, role: str, content: Any, meta: dict | None = None) -> str:
        """从 ``at_node_id`` 分支：新节点挂在该节点下并成为新叶（不复制节点）。"""
        if role not in _ROLES:
            raise ValueError(f"非法 role {role!r}")
        tree = self._load(sid)
        if at_node_id not in tree["nodes"]:
            raise ValueError(f"分支点 {at_node_id!r} 不存在")
        node_id = self._id_fn()
        tree["nodes"][node_id] = self._node(node_id, at_node_id, role, content, meta=meta)
        tree["leaf"] = node_id
        self._save(sid, tree)
        return node_id

    def leaf(self, sid: str) -> dict[str, Any] | None:
        """当前叶节点（None = 会话不存在）。"""
        tree = self._load(sid)
        if tree is None:
            return None
        return tree["nodes"].get(tree["leaf"])

    def path(self, sid: str, *, node_id: str | None = None) -> list[dict[str, Any]]:
        """根→node_id（默认当前叶）的消息链：LLM 上下文重建 / 时空回溯。"""
        tree = self._load(sid)
        if tree is None:
            return []
        target = node_id or tree["leaf"]
        if target not in tree["nodes"]:
            return []
        chain: list[dict[str, Any]] = []
        cur: str | None = target
        while cur is not None:
            node = tree["nodes"][cur]
            chain.append(dict(node))
            cur = node.get("parent_id")
        chain.reverse()
        return chain

    def messages(self, sid: str) -> list[dict[str, Any]]:
        """当前叶路径的 LLM 消息形态（protocol_translate 直接可用）。
        tool 节点 → OpenAI 协议 {role: tool, tool_call_id, content}。"""
        out: list[dict[str, Any]] = []
        for node in self.path(sid):
            role = node["role"]
            if role == "tool":
                meta = node.get("meta") or {}
                out.append({
                    "role": "tool",
                    "tool_call_id": meta.get("tool_call_id", ""),
                    "content": node.get("content") or "",
                })
                continue
            msg: dict[str, Any] = {"role": role, "content": node.get("content")}
            if node.get("tool_calls"):
                msg["tool_calls"] = _to_openai_tool_calls(node["tool_calls"])
            out.append(msg)
        return out



    def fork(self, sid: str, *, at_node_id: str) -> str:
        """时空回溯：从 ``at_node_id`` 复制整棵前缀树为新会话，该节点为新叶。"""
        tree = self._load(sid)
        if tree is None:
            raise ValueError(f"会话 {sid!r} 不存在")
        if at_node_id not in tree["nodes"]:
            raise ValueError(f"回溯点 {at_node_id!r} 不存在")
        # 复制根→at_node 链上的节点（深拷贝内容）
        new_sid = self._id_fn()
        prefix: dict[str, dict[str, Any]] = {}
        cur: str | None = at_node_id
        new_parent: str | None = None
        new_leaf = ""
        # 从根往下重建 parent 链
        chain: list[str] = []
        c: str | None = at_node_id
        while c is not None:
            chain.append(c)
            c = tree["nodes"][c].get("parent_id")
        chain.reverse()
        for old_id in chain:
            old = tree["nodes"][old_id]
            new_id = self._id_fn()
            node = self._node(
                new_id, new_parent, old["role"], old.get("content"),
                tool_calls=old.get("tool_calls"), meta=dict(old.get("meta") or {}),
            )
            prefix[new_id] = node
            new_parent = new_id
            new_leaf = new_id
        self._save(new_sid, {"root": new_leaf if new_parent is None else list(prefix)[0], "leaf": new_leaf, "nodes": prefix})
        return new_sid

    # ------------------------------------------------------------------ 持久化

    def snapshot(self, sid: str) -> dict[str, Any] | None:
        """整树快照（时空回溯恢复点，经 oprim.snapshot → KvStore）。"""
        tree = self._load(sid)
        if tree is None:
            return None
        return {"sid": sid, "tree": tree}

    def restore(self, sid: str, data: dict[str, Any]) -> None:
        """从快照原子恢复（覆盖当前会话树）。"""
        tree = data.get("tree")
        if not isinstance(tree, dict) or "nodes" not in tree:
            raise ValueError("快照数据非法: 缺少 tree.nodes")
        self._save(sid, tree)

    def delete(self, sid: str) -> None:
        """删除会话树快照。"""
        snapshot_delete(sid, kv=self._kv)

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _node(
        node_id: str, parent_id: str | None, role: str, content: Any,
        tool_calls: list | None = None, meta: dict | None = None,
    ) -> dict[str, Any]:
        return {
            "id": node_id,
            "parent_id": parent_id,
            "role": role,
            "content": content,
            "tool_calls": tool_calls or [],
            "meta": meta or {},
        }

    def _load(self, sid: str) -> dict[str, Any] | None:
        return snapshot_fetch(sid, kv=self._kv)

    def _save(self, sid: str, tree: dict[str, Any]) -> None:
        snapshot_commit(sid, tree, kv=self._kv)


__all__ = ["SessionTreeMgr"]


def _to_openai_tool_calls(tool_calls: list) -> list:
    """扁平 tool_calls（llm_message_to_agent 产出）→ OpenAI 协议原始形态。"""
    out: list[dict[str, Any]] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        if "function" in tc:
            out.append(tc)  # 已是原始形态
            continue
        args = tc.get("arguments")
        if isinstance(args, dict):
            import json

            args = json.dumps(args, ensure_ascii=False)
        out.append({
            "id": tc.get("id", ""),
            "type": "function",
            "function": {"name": tc.get("name", ""), "arguments": args or ""},
        })
    return out

