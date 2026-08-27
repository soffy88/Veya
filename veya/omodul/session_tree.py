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

import time
import uuid
from collections.abc import Callable
from typing import Any

from veya.oprim.snapshot import (
    snapshot_commit,
    snapshot_delete,
    snapshot_fetch,
    snapshot_list,
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

    def create_session(self, *, system: str | None = None, owner: str | None = None) -> str:
        """创建新会话（根节点 = system 提示，可省略）。返回 session_id。

        owner: 会话归属 (通常是已鉴权 user_id)；None = 不记归属 (旧调用方
        兼容，行为不变——校验逻辑见 ensure_session/owner_of)。
        """
        sid = self._id_fn()
        root_id = self._id_fn()
        nodes: dict[str, dict[str, Any]] = {}
        if system is not None:
            nodes[root_id] = self._node(root_id, None, "system", system)
        else:
            nodes[root_id] = self._node(root_id, None, "system", "")
        self._save(sid, {"root": root_id, "leaf": root_id, "nodes": nodes, "owner": owner})
        return sid

    def ensure_session(
        self, sid: str, *, system: str | None = None, owner: str | None = None
    ) -> str:
        """确保会话存在：不存在则用指定 sid 创建（外部传入 session_id 兼容）。

        已存在则校验归属：owner 非空且与树上记录的 owner 不同 → 拒绝
        (PermissionError)，防止拿到/猜到别人的 sid 就能续接读写其会话树
        (2026-08-16 修复——此前这里完全没有归属概念)。旧数据 (owner 字段
        缺失, 早于本次修复写入) 视为无主，放行但不会补写 owner，避免
        静默把无主数据据为己有。
        """
        tree = self._load(sid)
        if tree is None:
            root_id = self._id_fn()
            nodes = {root_id: self._node(root_id, None, "system", system or "")}
            self._save(sid, {"root": root_id, "leaf": root_id, "nodes": nodes, "owner": owner})
            return sid
        existing_owner = tree.get("owner")
        if owner is not None and existing_owner is not None and existing_owner != owner:
            raise PermissionError(f"会话 {sid!r} 不属于当前账号")
        return sid

    def owner_of(self, sid: str) -> str | None:
        """会话归属查询；会话不存在或未记归属都返回 None。"""
        tree = self._load(sid)
        return None if tree is None else tree.get("owner")

    def list_sessions(self, *, owner: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """列出会话摘要 (sid/title/msg_count/updated_at/owner)，按最近更新倒序。

        owner 非 None 时只返回该账号名下的会话 (2026-08-16 新增: 跨端同步
        /api/v1/agent/sessions 的数据源——此前该接口读的是完全独立的
        history_store.py, 而新主链 (VEYA_AGENT_LOOP=strict) 的对话只写进
        这里, 两边从未打通, 导致"多端同步"对新主链产生的对话完全不生效)。
        全量扫描所有快照 (snapshot_list) 逐个过滤——量级假设是单机/小规模
        部署, 会话数上千之前不需要额外索引。
        """
        out: list[dict[str, Any]] = []
        for sid in snapshot_list(kv=self._kv):
            tree = self._load(sid)
            if tree is None:
                continue
            if owner is not None and tree.get("owner") != owner:
                continue
            nodes = tree.get("nodes") or {}
            title = ""
            updated_at = 0.0
            msg_count = 0
            for node in nodes.values():
                role = node.get("role")
                if role in ("user", "assistant", "tool"):
                    msg_count += 1
                updated_at = max(updated_at, float(node.get("ts") or 0))
                if role == "user" and not title:
                    content = node.get("content")
                    title = str(content or "")[:40].replace("\n", " ")
            out.append(
                {
                    "sid": sid,
                    "title": title or "Untitled",
                    "msg_count": msg_count,
                    "updated_at": updated_at,
                    "owner": tree.get("owner"),
                }
            )
        out.sort(key=lambda s: s["updated_at"], reverse=True)
        return out[:limit]

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

    def branch(
        self, sid: str, *, at_node_id: str, role: str, content: Any, meta: dict | None = None
    ) -> str:
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

    def list_branches(self, sid: str) -> list[list[dict[str, Any]]]:
        """列出这棵树里所有分支（每个无子节点的叶子各一条根→叶路径）。

        对标 Maka"事实源不可变, 一切都是投影"的原则(见 memory
        project_veya_pi_gap_audit): coordinator_master 的 Compaction 头部重写
        走 branch(不覆盖旧节点), 但 `path()`/`leaf()` 只看当前活跃叶——旧分支
        (比如压缩前的完整原文)技术上还在树里, 只是没有入口找到它。这个方法
        就是那个入口: 只读, 不改任何状态, 供排障/审计/找回被压缩的原文用。
        """
        tree = self._load(sid)
        if tree is None:
            return []
        nodes = tree["nodes"]
        has_child: set[str] = set()
        for node in nodes.values():
            parent = node.get("parent_id")
            if parent is not None:
                has_child.add(parent)
        leaf_ids = [nid for nid in nodes if nid not in has_child]
        return [self.path(sid, node_id=lid) for lid in leaf_ids]

    def messages(self, sid: str) -> list[dict[str, Any]]:
        """当前叶路径的 LLM 消息形态（protocol_translate 直接可用）。
        tool 节点 → OpenAI 协议 {role: tool, tool_call_id, content}。"""
        out: list[dict[str, Any]] = []
        for node in self.path(sid):
            role = node["role"]
            if role == "tool":
                meta = node.get("meta") or {}
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": meta.get("tool_call_id", ""),
                        "content": node.get("content") or "",
                    }
                )
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
                new_id,
                new_parent,
                old["role"],
                old.get("content"),
                tool_calls=old.get("tool_calls"),
                meta=dict(old.get("meta") or {}),
            )
            prefix[new_id] = node
            new_parent = new_id
            new_leaf = new_id
        self._save(
            new_sid,
            {
                "root": new_leaf if new_parent is None else next(iter(prefix)),
                "leaf": new_leaf,
                "nodes": prefix,
                "owner": tree.get("owner"),  # 分支延续原会话归属
            },
        )
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
        node_id: str,
        parent_id: str | None,
        role: str,
        content: Any,
        tool_calls: list | None = None,
        meta: dict | None = None,
    ) -> dict[str, Any]:
        return {
            "id": node_id,
            "parent_id": parent_id,
            "role": role,
            "content": content,
            "tool_calls": tool_calls or [],
            "meta": meta or {},
            # 会话列表排序/展示用 (2026-08-16 补充: 此前节点没有时间戳, 无法
            # 按"最近更新"排会话列表——旧数据没有这个字段, 见 list_sessions
            # 里对缺失 ts 的兜底)。
            "ts": time.time(),
        }

    def _load(self, sid: str) -> dict[str, Any] | None:
        return snapshot_fetch(sid, kv=self._kv)

    def _save(self, sid: str, tree: dict[str, Any]) -> None:
        snapshot_commit(sid, tree, kv=self._kv)


_default_mirror: SessionTreeMgr | None = None


def default_session_tree_mirror() -> SessionTreeMgr:
    """主链历史镜像树单例，落 ~/.veya/sessions/session_tree_mirror.db（重启不丢）。

    与 agent_loop_bridge.py 的 ~/.veya/loop/session_tree.db 是不同文件——
    那棵树服务 agent_loop_run 隔离子任务，sid 命名空间不应与主链混用。
    """
    global _default_mirror
    if _default_mirror is None:
        from pathlib import Path

        from veya.obase.adapters import SqliteKvStore

        path = Path.home() / ".veya" / "sessions" / "session_tree_mirror.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        _default_mirror = SessionTreeMgr(kv=SqliteKvStore(str(path)))
    return _default_mirror


__all__ = ["SessionTreeMgr", "default_session_tree_mirror"]


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
        out.append(
            {
                "id": tc.get("id", ""),
                "type": "function",
                "function": {"name": tc.get("name", ""), "arguments": args or ""},
            }
        )
    return out
