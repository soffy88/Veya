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

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from veya.obase.async_utils import run_sync_in_daemon_thread

_SQLITE_BUSY_TIMEOUT_MS = 5_000


class SqliteHistoryStore:
    """按 session_id 存取对话消息列表; WAL + 进程内写锁, 低并发够用。"""

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
                "CREATE TABLE IF NOT EXISTS turns ("
                "  user_id TEXT NOT NULL DEFAULT 'anonymous',"
                "  sid TEXT NOT NULL,"
                "  idx INTEGER NOT NULL,"
                "  msg_json TEXT NOT NULL,"
                "  ts INTEGER NOT NULL,"
                "  PRIMARY KEY (user_id, sid, idx)"
                ")"
            )
            # 旧库迁移: 补 user_id 列 (幂等)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(turns)").fetchall()]
            if "user_id" not in cols:
                conn.execute("ALTER TABLE turns ADD COLUMN user_id TEXT DEFAULT 'anonymous'")
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_uid_sid ON turns(user_id, sid, idx)"
                )

    # ── sync core ────────────────────────────────────────────────────
    @staticmethod
    def _uid() -> str:
        """当前请求用户 (auth contextvar); 无请求上下文时回落 anonymous。"""
        try:
            from server.auth import current_user

            return current_user()["user_id"]
        except Exception:
            return "anonymous"

    def load_sync(self, sid: str, user_id: str | None = None) -> list[dict[str, Any]]:
        uid = user_id or self._uid()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT msg_json FROM turns WHERE user_id=? AND sid=? ORDER BY idx",
                (uid, sid),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for (raw,) in rows:
            try:
                out.append(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                continue  # 单条损坏不拖垮整段历史
        return out

    def save_sync(self, sid: str, messages: list[dict[str, Any]], user_id: str | None = None) -> None:
        uid = user_id or self._uid()
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM turns WHERE user_id=? AND sid=?", (uid, sid))
            conn.executemany(
                "INSERT INTO turns (user_id, sid, idx, msg_json, ts) VALUES (?,?,?,?,?)",
                [
                    (uid, sid, i, json.dumps(m, ensure_ascii=False, default=str), now)
                    for i, m in enumerate(messages)
                ],
            )

    def list_sessions_sync(self, user_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """按用户列出会话: sid / title(首条用户消息) / msg_count / updated_at。"""
        uid = user_id or self._uid()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT sid, msg_json, ts FROM turns WHERE user_id=? "
                "ORDER BY ts DESC LIMIT ?",
                (uid, limit * 40),
            ).fetchall()
        agg: dict[str, dict[str, Any]] = {}
        for sid, raw, ts in rows:
            item = agg.setdefault(sid, {"sid": sid, "title": "", "msg_count": 0, "updated_at": ts})
            item["msg_count"] += 1
            item["updated_at"] = max(item["updated_at"], ts)
            if not item["title"]:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if msg.get("role") == "user":
                    content = msg.get("content")
                    if isinstance(content, list):
                        content = "".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    text = str(content or "").replace("\n", " ").strip()
                    item["title"] = text[:40] or item["title"]
        out = sorted(agg.values(), key=lambda s: s["updated_at"], reverse=True)
        return out[:limit]

    # ── async wrappers (不阻塞事件循环) ──────────────────────────────
    async def load(self, sid: str, user_id: str | None = None) -> list[dict[str, Any]]:
        return await run_sync_in_daemon_thread(self.load_sync, sid, user_id)

    async def save(self, sid: str, messages: list[dict[str, Any]], user_id: str | None = None) -> None:
        await run_sync_in_daemon_thread(self.save_sync, sid, messages, user_id)

    async def list_sessions(self, user_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return await run_sync_in_daemon_thread(self.list_sessions_sync, user_id, limit)


_default_store: SqliteHistoryStore | None = None


def default_history_store() -> SqliteHistoryStore:
    """进程级单例, 落 ~/.veya/sessions/history.db (veya-data 卷, 重启不丢)。"""
    global _default_store
    if _default_store is None:
        _default_store = SqliteHistoryStore(Path.home() / ".veya" / "sessions" / "history.db")
    return _default_store
