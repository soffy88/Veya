"""veya/memory_store.py — 蒸馏记忆存储 + 检索 (P4 个人智能层).

对话历史 (P1) 解决"不失忆"; 本模块解决"懂你": 把对话蒸馏出的**持久事实 /
偏好 / 摘要**存下, 新一轮按相关性检索 top-K 注入 —— 不倒带全量 transcript。

存储: `~/.veya/memory/memory.db` (SQLite, veya-data 卷)。
检索: 默认**零依赖** 关键词重叠 + recency + salience 打分 (无需 numpy/embedding);
      未来可注入 embedder / 复用 stratum 做语义检索 (见 CONTEXT_MEMORY_DESIGN.md P4)。
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
                "  ts INTEGER NOT NULL"
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
    ) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        with self._lock, self._connect() as conn:
            # 去重: 同 user 同 text 已存在 → 抬 salience + 刷新 ts, 不新增
            row = conn.execute(
                "SELECT id, salience FROM memories WHERE user_id=? AND text=?",
                (user_id, text),
            ).fetchone()
            now = int(time.time())
            if row:
                mid, old = row
                conn.execute(
                    "UPDATE memories SET salience=?, ts=? WHERE id=?",
                    (min(1.0, float(old) + 0.1), now, mid),
                )
                return mid
            mid = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO memories (id, user_id, kind, text, salience, source_sid, ts)"
                " VALUES (?,?,?,?,?,?,?)",
                (mid, user_id, kind, text, float(salience), source_sid, now),
            )
            return mid

    def all_for_user_sync(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, kind, text, salience, source_sid, ts FROM memories WHERE user_id=?",
                (user_id,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "kind": r[1],
                "text": r[2],
                "salience": r[3],
                "source_sid": r[4],
                "ts": r[5],
            }
            for r in rows
        ]

    def retrieve_sync(self, user_id: str, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        """关键词重叠 + recency + salience 加权; 无 embedding 依赖。"""
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
        return await run_sync_in_daemon_thread(self.add_sync, user_id, kind, text, **kw)

    async def retrieve(self, user_id: str, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        return await run_sync_in_daemon_thread(self.retrieve_sync, user_id, query, top_k=top_k)


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
