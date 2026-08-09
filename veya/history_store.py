"""veya/history_store.py — durable conversation history (P1 强上下文地基).

主脑 (oservi MasterAgent) 的对话历史原为**纯进程内 dict** (`_histories`),
`git pull → docker compose up -d` 重启即清空 → 失忆。本模块提供一个**进程无关**
的 SQLite 持久层, 跑在 `~/.veya/` (veya-data 命名卷, 重启不丢)。

装配范式 (§1.4): 机制抽象 = `HistoryStore` 协议; 具体存储 (SQLite) 由 veya 注入。
进程内 dict 降级为热缓存, 本 store 为权威源 (冷启动/换进程从它恢复)。

存储的是**非 system 消息** (system prompt 每次由主库用当前版本重建, 避免存旧提示词)。
每条消息以 JSON 原样存 (保留 role/content/tool_calls/tool_call_id 全字段)。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class SqliteHistoryStore:
    """按 session_id 存取对话消息列表; WAL + 进程内写锁, 低并发够用。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS turns ("
                "  sid TEXT NOT NULL,"
                "  idx INTEGER NOT NULL,"
                "  msg_json TEXT NOT NULL,"
                "  ts INTEGER NOT NULL,"
                "  PRIMARY KEY (sid, idx)"
                ")"
            )

    # ── sync core ────────────────────────────────────────────────────
    def load_sync(self, sid: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT msg_json FROM turns WHERE sid=? ORDER BY idx", (sid,)
            ).fetchall()
        out: list[dict[str, Any]] = []
        for (raw,) in rows:
            try:
                out.append(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                continue  # 单条损坏不拖垮整段历史
        return out

    def save_sync(self, sid: str, messages: list[dict[str, Any]]) -> None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM turns WHERE sid=?", (sid,))
            conn.executemany(
                "INSERT INTO turns (sid, idx, msg_json, ts) VALUES (?,?,?,?)",
                [
                    (sid, i, json.dumps(m, ensure_ascii=False, default=str), now)
                    for i, m in enumerate(messages)
                ],
            )

    # ── async wrappers (不阻塞事件循环) ──────────────────────────────
    async def load(self, sid: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.load_sync, sid)

    async def save(self, sid: str, messages: list[dict[str, Any]]) -> None:
        await asyncio.to_thread(self.save_sync, sid, messages)


_default_store: SqliteHistoryStore | None = None


def default_history_store() -> SqliteHistoryStore:
    """进程级单例, 落 ~/.veya/sessions/history.db (veya-data 卷, 重启不丢)。"""
    global _default_store
    if _default_store is None:
        _default_store = SqliteHistoryStore(Path.home() / ".veya" / "sessions" / "history.db")
    return _default_store
