"""veya/history_store.py — durable conversation history (P1 强上下文地基).

主脑 (oservi MasterAgent) 的对话历史原为**纯进程内 dict** (`_histories`),
`git pull → docker compose up -d` 重启即清空 → 失忆。本模块提供一个**进程无关**
的 SQLite 持久层, 跑在 `~/.veya/` (veya-data 命名卷, 重启不丢)。

装配范式 (§1.4): 机制抽象 = `HistoryStore` 协议; 具体存储 (SQLite) 由 veya 注入。
进程内 dict 降级为热缓存, 本 store 为权威源 (冷启动/换进程从它恢复)。

存储的是**非 system 消息** (system prompt 每次由主库用当前版本重建, 避免存旧提示词)。

2026-08-24 (docs/dev/rfc-11-state-authority-scoping.md 后续, 用户明确"不用考虑
之前的数据"授权直接改存储模型): `save()` 从"整段覆盖(DELETE+INSERT)"改成
"追加一条不可变修订(INSERT-only)"——`turns` 表每一行现在是某个 session 在
某个修订号(revision)时刻的**完整消息列表快照**, 旧修订永远不删/不改。
`load()` 外部行为不变(仍是"读当前应该看到的消息列表"), 内部变成"读最新一条
修订"; 新增 `replay()` 读完整的不可变修订序列(canonical event log 的读侧)。
这是 docs/VEYA_10_OF_10_PLAN.md §9.1"原始事实不可变, 派生状态可以重建"原则
第一个真正落地的存储, 范围只到会话历史这一项, session_tree/memory 暂不动
(见 rfc-11 §3, 那两个要不要同样改是单独的决策)。

已知取舍: 存储不再原地覆盖, 长会话频繁 checkpoint (VEYA_CHECKPOINT_INTERVAL_S)
会让同一 session 的行数随时间增长, 没做修订保留策略(比如"只留最近 N 条 + 定期
归档更早的")——这是故意先不做的部分, 不是漏做, 现在不知道合适的保留窗口该多大,
留给用量数据出来之后再定, 强行猜一个数字比不设限更容易做错决定。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from veya.obase.async_utils import run_sync_in_daemon_thread

_SQLITE_BUSY_TIMEOUT_MS = 5_000
_user_id_provider: ContextVar[Callable[[], dict[str, Any]] | None] = ContextVar(
    "history_user_id_provider", default=None
)


def set_user_id_provider(provider: Callable[[], dict[str, Any]] | None) -> None:
    """Install the host's request-user callback without importing the host layer.

    ``veya.oservi`` is deliberately below ``server`` in the 3O dependency graph.
    The server auth module injects its context-local callback at assembly time;
    standalone/CLI use therefore keeps the anonymous fallback.
    """
    _user_id_provider.set(provider)


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
            # revision: 每次 save() 递增, INSERT-only, 旧 revision 永不删/改
            # (canonical event log; 见文件头 2026-08-24 说明)。msg_json 是该
            # revision 时刻的完整消息列表快照, 不是单条消息。
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(turns)").fetchall()
            }
            if columns and "revision" not in columns:
                # 0.6 的 turns(sid, idx, msg_json, ts, user_id) 是可迁移的旧
                # projection。保留每条旧快照的顺序，转成新的 user/sid/revision
                # 主键；不 silently drop 用户历史。
                conn.execute("ALTER TABLE turns RENAME TO turns_legacy")
                conn.execute(
                    "CREATE TABLE turns ("
                    "  user_id TEXT NOT NULL DEFAULT 'anonymous',"
                    "  sid TEXT NOT NULL,"
                    "  revision INTEGER NOT NULL,"
                    "  msg_json TEXT NOT NULL,"
                    "  ts INTEGER NOT NULL,"
                    "  PRIMARY KEY (user_id, sid, revision)"
                    ")"
                )
                conn.execute(
                    "INSERT INTO turns (user_id, sid, revision, msg_json, ts) "
                    "SELECT COALESCE(user_id, 'anonymous'), sid, idx, msg_json, ts "
                    "FROM turns_legacy ORDER BY user_id, sid, idx"
                )
                conn.execute("DROP TABLE turns_legacy")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS turns ("
                "  user_id TEXT NOT NULL DEFAULT 'anonymous',"
                "  sid TEXT NOT NULL,"
                "  revision INTEGER NOT NULL,"
                "  msg_json TEXT NOT NULL,"
                "  ts INTEGER NOT NULL,"
                "  PRIMARY KEY (user_id, sid, revision)"
                ")"
            )

    # ── sync core ────────────────────────────────────────────────────
    @staticmethod
    def _uid() -> str:
        """当前请求用户 (auth contextvar); 无请求上下文时回落 anonymous。"""
        try:
            provider = _user_id_provider.get()
            if provider is not None:
                return str(provider().get("user_id") or "anonymous")
        except Exception:
            pass
        return "anonymous"

    def load_sync(self, sid: str, user_id: str | None = None) -> list[dict[str, Any]]:
        """当前投影: 最新一条修订的消息列表快照(外部行为跟改动前完全一致)。"""
        uid = user_id or self._uid()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT msg_json FROM turns WHERE user_id=? AND sid=? "
                "ORDER BY revision DESC LIMIT 1",
                (uid, sid),
            ).fetchone()
        if row is None:
            return []
        try:
            loaded = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return []
        return loaded if isinstance(loaded, list) else []

    def save_sync(
        self, sid: str, messages: list[dict[str, Any]], user_id: str | None = None
    ) -> None:
        """追加一条新修订(INSERT-only)——不覆盖、不删除任何既有修订。"""
        uid = user_id or self._uid()
        now = int(time.time())
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(revision), -1) FROM turns WHERE user_id=? AND sid=?",
                (uid, sid),
            ).fetchone()
            next_revision = row[0] + 1
            conn.execute(
                "INSERT INTO turns (user_id, sid, revision, msg_json, ts) VALUES (?,?,?,?,?)",
                (
                    uid,
                    sid,
                    next_revision,
                    json.dumps(messages, ensure_ascii=False, default=str),
                    now,
                ),
            )

    def replay_sync(
        self, sid: str, user_id: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """不可变修订序列(canonical event log 的读侧, §9.1) —— 按修订号升序,
        每项 {"revision", "messages", "ts"}。limit 时只取最早的 N 条(诊断
        用途, 不是"最近 N 条", 需要最近的直接用 load_sync 拿最新一条即可)。"""
        uid = user_id or self._uid()
        query = (
            "SELECT revision, msg_json, ts FROM turns WHERE user_id=? AND sid=? ORDER BY revision"
        )
        params: list[Any] = [uid, sid]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        out: list[dict[str, Any]] = []
        for revision, raw, ts in rows:
            try:
                messages = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            out.append({"revision": revision, "messages": messages, "ts": ts})
        return out

    def list_sessions_sync(
        self, user_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """按用户列出会话: sid / title(首条用户消息) / msg_count / updated_at。

        每个 sid 只看最新一条修订(窗口函数按 revision 倒序取第一行) —— msg_count
        是当前投影的消息数, 不是历史修订行数。
        """
        uid = user_id or self._uid()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT sid, msg_json, ts FROM ("
                "  SELECT sid, msg_json, ts,"
                "         ROW_NUMBER() OVER (PARTITION BY sid ORDER BY revision DESC) AS rn"
                "  FROM turns WHERE user_id=?"
                ") WHERE rn = 1 ORDER BY ts DESC LIMIT ?",
                (uid, limit),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for sid, raw, ts in rows:
            try:
                messages = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                messages = []
            title = ""
            for msg in messages if isinstance(messages, list) else []:
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if isinstance(content, list):
                    content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                text = str(content or "").replace("\n", " ").strip()
                if text:
                    title = text[:40]
                    break
            out.append({"sid": sid, "title": title, "msg_count": len(messages), "updated_at": ts})
        return sorted(out, key=lambda s: s["updated_at"], reverse=True)[:limit]

    # ── async wrappers (不阻塞事件循环) ──────────────────────────────
    async def load(self, sid: str, user_id: str | None = None) -> list[dict[str, Any]]:
        return list(await run_sync_in_daemon_thread(self.load_sync, sid, user_id))

    async def save(
        self, sid: str, messages: list[dict[str, Any]], user_id: str | None = None
    ) -> None:
        await run_sync_in_daemon_thread(self.save_sync, sid, messages, user_id)

    async def replay(
        self, sid: str, user_id: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        return list(await run_sync_in_daemon_thread(self.replay_sync, sid, user_id, limit))

    async def list_sessions(
        self, user_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return list(await run_sync_in_daemon_thread(self.list_sessions_sync, user_id, limit))


_default_store: SqliteHistoryStore | None = None


def default_history_store() -> SqliteHistoryStore:
    """进程级单例, 落 ~/.veya/sessions/history.db (veya-data 卷, 重启不丢)。"""
    global _default_store
    if _default_store is None:
        _default_store = SqliteHistoryStore(Path.home() / ".veya" / "sessions" / "history.db")
    return _default_store
