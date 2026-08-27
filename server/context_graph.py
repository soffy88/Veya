"""context_graph — 轻量上下文图 (Semantica Context Graph 内化, 2026-08-16)。

图遍历记忆: 实体/关系是显式节点, 回答「什么相连、为什么、怎么连」——
向量检索的补充而非替代。本实现轻量自持:
- SQLite 邻接表 (nodes + edges), stdlib 零依赖
- 软删 (deleted_at) → 时点快照 state_at(ts) 可回放
- 遍历: graph_neighbors(nid, hops) BFS 子图
- 与决策账本联动: 决策 id 可作节点, parent 边即因果链

接入: 主脑经 graph_* 工具自主查询 (模型自主选择, 零程序路由);
project_ask 把任务/追问自动建节点与因果边。
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

_DB_PATH = os.environ.get("VEYA_CONTEXT_GRAPH_DB", str(Path.home() / ".veya" / "context_graph.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    props       TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    deleted_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE TABLE IF NOT EXISTS edges (
    src         TEXT NOT NULL,
    rel         TEXT NOT NULL,
    dst         TEXT NOT NULL,
    props       TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    deleted_at  TEXT,
    PRIMARY KEY (src, rel, dst)
);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
"""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ContextGraph:
    """轻量上下文图 (SQLite 邻接表, check_same_thread=False 与 auth.py 同模式)。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._path = str(db_path or _DB_PATH)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── 写 ─────────────────────────────────────────────────────────
    def upsert_node(
        self, node_id: str, kind: str, name: str, props: dict[str, Any] | None = None
    ) -> None:
        """建/更新节点。已软删的节点复活 (清 deleted_at)。"""
        now = _now()
        self._conn.execute(
            "INSERT INTO nodes (id, kind, name, props, created_at, deleted_at)"
            " VALUES (?,?,?,?,?,NULL)"
            " ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, name=excluded.name,"
            " props=excluded.props, deleted_at=NULL",
            (
                node_id,
                kind[:50],
                name[:500],
                json.dumps(props or {}, ensure_ascii=False)[:8000],
                now,
            ),
        )
        self._conn.commit()

    def add_edge(self, src: str, rel: str, dst: str, props: dict[str, Any] | None = None) -> None:
        """加有向边 (rel 小写归一)。重复边更新 props + 复活。"""
        now = _now()
        self._conn.execute(
            "INSERT INTO edges (src, rel, dst, props, created_at, deleted_at)"
            " VALUES (?,?,?,?,?,NULL)"
            " ON CONFLICT(src, rel, dst) DO UPDATE SET props=excluded.props, deleted_at=NULL",
            (
                src,
                rel.strip().lower()[:80],
                dst,
                json.dumps(props or {}, ensure_ascii=False)[:8000],
                now,
            ),
        )
        self._conn.commit()

    def remove_node(self, node_id: str) -> None:
        """软删节点 (含其出边); 时点快照仍可回放。"""
        now = _now()
        self._conn.execute("UPDATE nodes SET deleted_at = ? WHERE id = ?", (now, node_id))
        self._conn.execute("UPDATE edges SET deleted_at = ? WHERE src = ?", (now, node_id))
        self._conn.commit()

    # ── 读 ─────────────────────────────────────────────────────────
    def get_node(self, node_id: str, *, as_of: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM nodes WHERE id = ?"
        params: list[Any] = [node_id]
        if as_of:
            # 时点视角: 该时刻未被删除 (删除时间晚于 as_of 或从未删)
            sql += " AND (deleted_at IS NULL OR deleted_at > ?)"
            params.append(as_of)
        else:
            # 当前视角: 只返回存活节点
            sql += " AND deleted_at IS NULL"
        row = self._conn.execute(sql, params).fetchone()
        return _row_to_dict(row) if row else None

    def neighbors(self, node_id: str, *, hops: int = 1, as_of: str | None = None) -> dict[str, Any]:
        """BFS 遍历子图 (出边 + 入边), 返回 {nodes, edges, hops}。"""
        if hops < 1:
            hops = 1
        if hops > 4:
            hops = 4  # 防爆炸
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[tuple[str, str, str], dict[str, Any]] = {}  # (src,rel,dst) 去重
        frontier = {node_id}
        visited: set[str] = set()
        for _ in range(hops):
            nxt: set[str] = set()
            for nid in frontier:
                if nid in visited:
                    continue
                visited.add(nid)
                node = self.get_node(nid, as_of=as_of)
                if node:
                    nodes[nid] = node
                rows = self._conn.execute(
                    "SELECT src, rel, dst, props, created_at FROM edges"
                    " WHERE (src = ? OR dst = ?) AND (deleted_at IS NULL OR deleted_at > ?)",
                    (nid, nid, as_of or "9999-12-31"),
                ).fetchall()
                for r in rows:
                    edges[(r["src"], r["rel"], r["dst"])] = {
                        "src": r["src"],
                        "rel": r["rel"],
                        "dst": r["dst"],
                        "props": _loads(r["props"]),
                    }
                    nxt.add(r["src"])
                    nxt.add(r["dst"])
            frontier = nxt - visited
            if not frontier:
                break
        # 末跳发现的节点也收入 nodes (否则 hops 边界上的实体缺失)
        for nid in frontier:
            node = self.get_node(nid, as_of=as_of)
            if node:
                nodes[nid] = node
        return {
            "center": node_id,
            "hops": hops,
            "nodes": nodes,
            "edges": list(edges.values()),
        }

    def state_at(self, timestamp: str) -> dict[str, Any]:
        """时点快照: 该时刻仍存活的节点数/边数 + 最新一批节点。"""
        nodes = self._conn.execute(
            "SELECT * FROM nodes WHERE deleted_at IS NULL OR deleted_at > ? ORDER BY created_at DESC LIMIT 50",
            (timestamp,),
        ).fetchall()
        edges = self._conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE deleted_at IS NULL OR deleted_at > ?",
            (timestamp,),
        ).fetchone()
        return {
            "as_of": timestamp,
            "alive_nodes": len(nodes),
            "alive_edges": edges["c"] if edges else 0,
            "recent_nodes": [_row_to_dict(r) for r in nodes[:10]],
        }

    def summary(self, limit: int = 8) -> str:
        """人类可读摘要 (供记忆注入/诊断)。"""
        n = self._conn.execute("SELECT COUNT(*) c FROM nodes WHERE deleted_at IS NULL").fetchone()
        e = self._conn.execute("SELECT COUNT(*) c FROM edges WHERE deleted_at IS NULL").fetchone()
        recent = self._conn.execute(
            "SELECT * FROM nodes WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        lines = [f"上下文图: {n['c'] if n else 0} 节点 / {e['c'] if e else 0} 边"]
        for r in recent:
            d = _row_to_dict(r)
            lines.append(f"  • {d['kind']} {d['name']} ({d['id']})")
        return "\n".join(lines)

    def close(self) -> None:
        self._conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["props"] = _loads(d.get("props") or "{}")
    return d


def _loads(s: Any) -> dict[str, Any]:
    try:
        v = json.loads(s) if isinstance(s, str) else {}
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# ── 全局单例 ───────────────────────────────────────────────────────
graph = ContextGraph()


__all__ = [
    "ContextGraph",
    "graph",
    "upsert_node",
    "add_edge",
    "remove_node",
    "neighbors",
    "state_at",
    "summary",
]


def upsert_node(node_id: str, kind: str, name: str, props: dict[str, Any] | None = None) -> None:
    graph.upsert_node(node_id, kind, name, props)


def add_edge(src: str, rel: str, dst: str, props: dict[str, Any] | None = None) -> None:
    graph.add_edge(src, rel, dst, props)


def remove_node(node_id: str) -> None:
    graph.remove_node(node_id)


def neighbors(node_id: str, *, hops: int = 1) -> dict[str, Any]:
    return graph.neighbors(node_id, hops=hops)


def state_at(timestamp: str) -> dict[str, Any]:
    return graph.state_at(timestamp)


def summary(limit: int = 8) -> str:
    return graph.summary(limit=limit)
