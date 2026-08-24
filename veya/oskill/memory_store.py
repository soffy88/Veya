"""veya/memory_store.py — 蒸馏记忆存储 + 检索 (P4 个人智能层).

对话历史 (P1) 解决"不失忆"; 本模块解决"懂你": 把对话蒸馏出的**持久事实 /
偏好 / 摘要**存下, 新一轮按相关性检索 top-K 注入 —— 不倒带全量 transcript。

存储: `~/.veya/memory/memory.db` (SQLite, veya-data 卷)。
检索: 默认**零依赖** 关键词重叠 + recency + salience 打分 (无需 numpy/embedding);
      未来可注入 embedder / 复用 stratum 做语义检索 (见 CONTEXT_MEMORY_DESIGN.md P4)。

2026-08-24 (docs/dev/rfc-11-state-authority-scoping.md §9.4, "不能把模型总结
直接视为事实"): 补 confidence/scope/created_at + invalidate/supersede——之前
一条记忆一旦写入就永远被检索到, 没有"这条记忆错了/过时了"的表达方式。新增:
- confidence: 写入时可选声明"这条提炼有多确定", 缺省 1.0(向后兼容, 不强迫
  调用方填)。
- invalidate_sync(): 标记一条记忆不再检索, 不物理删除(保留审计轨迹)。
- supersede_sync(): invalidate 旧记忆 + 写入替换它的新记忆, 新记忆继承旧记忆
  id 作为溯源指针——查得到"这条记忆是从哪条记忆修正来的"。
source_sid 故意保持单数(不是计划文档写的 source_event_ids 复数列表)——现在
所有调用方每次只传一个 session id, 造一个没人填多值的列表字段是形式主义。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from veya.obase.async_utils import run_sync_in_daemon_thread

_WORD = re.compile(r"[\w一-鿿]+")
_SQLITE_BUSY_TIMEOUT_MS = 5_000


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _WORD.findall(text or "")}


class SqliteMemoryStore:
    """按 user_id 存取蒸馏记忆; 关键词+recency+salience 打分检索。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=_SQLITE_BUSY_TIMEOUT_MS / 1_000,
            check_same_thread=False,
        )
        conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            # journal_mode may need a database lock. Set it once during
            # initialization so read-only connections never wait on this PRAGMA.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memories ("
                "  id TEXT PRIMARY KEY,"
                "  user_id TEXT NOT NULL,"
                "  kind TEXT NOT NULL,"  # fact | preference | summary
                "  text TEXT NOT NULL,"
                "  salience REAL NOT NULL,"
                "  source_sid TEXT,"
                "  confidence REAL NOT NULL DEFAULT 1.0,"
                "  scope TEXT NOT NULL DEFAULT 'user',"
                "  created_at INTEGER NOT NULL,"
                "  invalidated INTEGER NOT NULL DEFAULT 0,"
                "  superseded_by TEXT,"
                "  ts INTEGER NOT NULL"  # 最近一次写入/去重触达时间 (即 last_verified_at)
                ")"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id)")

    # ── sync core ────────────────────────────────────────────────────
    def add_sync(
        self,
        user_id: str,
        kind: str,
        text: str,
        *,
        salience: float = 0.5,
        source_sid: str | None = None,
        confidence: float = 1.0,
        scope: str = "user",
    ) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        with self._lock, self._connect() as conn:
            # 去重: 同 user 同 text 已存在 → 抬 salience + 刷新 ts(=再次被印证), 不新增,
            # 不动 confidence/created_at(去重触达不是"重新评估确定性"或"重新创建")。
            row = conn.execute(
                "SELECT id, salience FROM memories WHERE user_id=? AND text=? AND invalidated=0",
                (user_id, text),
            ).fetchone()
            now = int(time.time())
            if row:
                mid, old = row
                conn.execute(
                    "UPDATE memories SET salience=?, ts=? WHERE id=?",
                    (min(1.0, float(old) + 0.1), now, mid),
                )
                return str(mid)
            mid = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO memories"
                " (id, user_id, kind, text, salience, source_sid, confidence, scope, created_at, ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    mid,
                    user_id,
                    kind,
                    text,
                    float(salience),
                    source_sid,
                    float(confidence),
                    scope,
                    now,
                    now,
                ),
            )
            return mid

    def invalidate_sync(self, memory_id: str) -> bool:
        """标记一条记忆不再检索(不物理删除, 保留审计轨迹)。返回是否真的改动了一行。"""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE memories SET invalidated=1 WHERE id=? AND invalidated=0", (memory_id,)
            )
            return cur.rowcount > 0

    def supersede_sync(
        self,
        old_id: str,
        user_id: str,
        kind: str,
        text: str,
        *,
        salience: float = 0.5,
        source_sid: str | None = None,
        confidence: float = 1.0,
        scope: str = "user",
    ) -> str:
        """废弃 old_id、写入替换它的新记忆, 新记忆 id 回填进 old_id 的
        superseded_by(溯源: 能查到"这条记忆是从哪条修正来的")。old_id 不存在
        或已经废弃时仍然正常写入新记忆(不因为溯源目标缺失就拒绝修正)。"""
        new_id = self.add_sync(
            user_id,
            kind,
            text,
            salience=salience,
            source_sid=source_sid,
            confidence=confidence,
            scope=scope,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE memories SET invalidated=1, superseded_by=? WHERE id=? AND invalidated=0",
                (new_id, old_id),
            )
        return new_id

    def all_for_user_sync(
        self, user_id: str, *, include_invalidated: bool = False
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT id, kind, text, salience, source_sid, confidence, scope,"
            " created_at, ts, invalidated, superseded_by FROM memories WHERE user_id=?"
        )
        params: list[Any] = [user_id]
        if not include_invalidated:
            query += " AND invalidated=0"
        with self._lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "id": r[0],
                "kind": r[1],
                "text": r[2],
                "salience": r[3],
                "source_sid": r[4],
                "confidence": r[5],
                "scope": r[6],
                "created_at": r[7],
                "ts": r[8],
                "last_verified_at": r[8],
                "invalidated": bool(r[9]),
                "superseded_by": r[10],
            }
            for r in rows
        ]

    def retrieve_sync(self, user_id: str, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        """关键词重叠 + recency + salience 加权; 无 embedding 依赖。已废弃
        (invalidate/supersede 过)的记忆不参与检索——all_for_user_sync 默认过滤。"""
        mems = self.all_for_user_sync(user_id)
        if not mems:
            return []
        q_tokens = _tokens(query)
        now = time.time()
        newest = max((m["ts"] for m in mems), default=now) or now
        oldest = min((m["ts"] for m in mems), default=now)
        span = max(1.0, float(newest - oldest))
        scored: list[tuple[float, dict[str, Any]]] = []
        for m in mems:
            m_tokens = _tokens(m["text"])
            overlap = len(q_tokens & m_tokens) / (len(q_tokens) + 1e-6) if q_tokens else 0.0
            recency = (float(m["ts"]) - oldest) / span  # 0..1
            score = 0.55 * overlap + 0.30 * float(m["salience"]) + 0.15 * recency
            scored.append((score, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:top_k]]

    # ── async wrappers ───────────────────────────────────────────────
    async def add(self, user_id: str, kind: str, text: str, **kw: Any) -> str:
        return str(await run_sync_in_daemon_thread(self.add_sync, user_id, kind, text, **kw))

    async def retrieve(self, user_id: str, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        return list(
            await run_sync_in_daemon_thread(self.retrieve_sync, user_id, query, top_k=top_k)
        )

    async def invalidate(self, memory_id: str) -> bool:
        return bool(await run_sync_in_daemon_thread(self.invalidate_sync, memory_id))

    async def supersede(self, old_id: str, user_id: str, kind: str, text: str, **kw: Any) -> str:
        return str(
            await run_sync_in_daemon_thread(self.supersede_sync, old_id, user_id, kind, text, **kw)
        )

    async def all_for_user(
        self, user_id: str, *, include_invalidated: bool = False
    ) -> list[dict[str, Any]]:
        return list(
            await run_sync_in_daemon_thread(
                self.all_for_user_sync, user_id, include_invalidated=include_invalidated
            )
        )


_default_store: SqliteMemoryStore | None = None


def default_memory_store() -> SqliteMemoryStore:
    """进程级单例, 落 ~/.veya/memory/memory.db (veya-data 卷, 重启不丢)。"""
    global _default_store
    if _default_store is None:
        _default_store = SqliteMemoryStore(Path.home() / ".veya" / "memory" / "memory.db")
    return _default_store


def format_memory_block(mems: list[dict[str, Any]]) -> str:
    """把 top-K 记忆压成一个紧凑注入块 (不是倒 transcript)。"""
    if not mems:
        return ""
    lines = ["# MEMORY (关于用户 — 你此前已了解的持久事实/偏好):"]
    for m in mems:
        tag = {"fact": "事实", "preference": "偏好", "summary": "背景"}.get(m["kind"], "记忆")
        lines.append(f"- [{tag}] {m['text']}")
    lines.append("(以上为你的长期记忆检索结果; 自然运用, 不要复述本清单。)")
    return "\n".join(lines)
