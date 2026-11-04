"""veya/obase/approvals — durable approval/grant/handoff records (NANO-W6).

The persistence substrate for the W6 approval gates. One SQLite database
(``~/.veya/approvals.db``, ``VEYA_APPROVALS_PATH`` overrides) with three
tables:

* ``approvals`` — one row per pending/resolved approval request. Keyed by a
  stable ``request_id``; the wait loop polls this table, so a request created
  on one runner is resolvable from any process that can read the same store.
* ``grants`` — "always allow" grants, one row per (scope, tool, scope_pattern).
* ``handoffs`` — human-handoff requests ("user must do this step") with
  Done/Cancel resume.
* ``turn_resumes`` — suspended turns waiting on approvals (Autoloop graft
  item 1): one row per suspension, claimed exactly once, expired by TTL.

Conventions follow the existing obase stores (``SqliteKvStore``): one
``check_same_thread=False`` connection behind a lock, JSON-encoded values,
atomic commits. This module never imports the 3O layers above obase.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_APPROVALS_PATH = "~/.veya/approvals.db"

STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"

DECISION_ONCE = "once"
DECISION_SESSION = "session"
DECISION_ALWAYS = "always"
DECISION_DENY = "deny"

HANDOFF_PENDING = "pending"
HANDOFF_DONE = "done"
HANDOFF_CANCELLED = "cancelled"


def default_approvals_path() -> Path:
    """Where the approval database lives (``VEYA_APPROVALS_PATH`` overrides)."""

    raw = os.environ.get("VEYA_APPROVALS_PATH") or DEFAULT_APPROVALS_PATH
    return Path(raw).expanduser()


class ApprovalStore:
    """SQLite-backed approvals, always-grants and handoffs."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = str(path) if path is not None else str(default_approvals_path())
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    request_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    tool_args_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    decision TEXT,
                    decision_reason TEXT,
                    delegate_policy TEXT,
                    session_id TEXT,
                    actor TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    resolved_at REAL
                );
                CREATE TABLE IF NOT EXISTS grants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    scope_pattern TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    UNIQUE(scope, tool, scope_pattern)
                );
                CREATE TABLE IF NOT EXISTS handoffs (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turn_resumes (
                    resume_id TEXT PRIMARY KEY,
                    bot_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    thread_id TEXT NOT NULL DEFAULT '',
                    request_ids_json TEXT NOT NULL DEFAULT '[]',
                    rev INTEGER NOT NULL DEFAULT 0,
                    objective_hash TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    claimed_at REAL,
                    expires_at REAL NOT NULL
                );
                """
            )
            self._conn.commit()

    # ── approvals ────────────────────────────────────────────────────
    def create_approval(self, record: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        row: dict[str, Any] = {
            "request_id": str(record["request_id"]),
            "scope": str(record.get("scope") or ""),
            "tool": str(record.get("tool") or ""),
            "tool_args_json": json.dumps(record.get("tool_args") or {}, ensure_ascii=False),
            "status": STATUS_PENDING,
            "decision": None,
            "decision_reason": None,
            "delegate_policy": str(record.get("delegate_policy") or ""),
            "session_id": str(record.get("session_id") or ""),
            "actor": str(record.get("actor") or ""),
            "created_at": float(record.get("created_at") or now),
            "updated_at": now,
            "resolved_at": None,
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO approvals
                (request_id, scope, tool, tool_args_json, status, decision,
                 decision_reason, delegate_policy, session_id, actor,
                 created_at, updated_at, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["request_id"],
                    row["scope"],
                    row["tool"],
                    row["tool_args_json"],
                    row["status"],
                    row["decision"],
                    row["decision_reason"],
                    row["delegate_policy"],
                    row["session_id"],
                    row["actor"],
                    row["created_at"],
                    row["updated_at"],
                    row["resolved_at"],
                ),
            )
            self._conn.commit()
        return self.get_approval(row["request_id"]) or row

    def get_approval(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE request_id=?", (str(request_id),)
            ).fetchone()
        return self._to_approval(row) if row is not None else None

    def list_approvals(
        self,
        *,
        scope: str = "",
        status: str = "",
        tool: str = "",
        session_id: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM approvals WHERE 1=1"
        params: list[str] = []
        if scope:
            query += " AND scope=?"
            params.append(scope)
        if status:
            query += " AND status=?"
            params.append(status)
        if tool:
            query += " AND tool=?"
            params.append(tool)
        if session_id:
            query += " AND session_id=?"
            params.append(session_id)
        query += f" ORDER BY created_at DESC LIMIT {max(1, min(int(limit), 500))}"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._to_approval(row) for row in rows if row is not None]

    def resolve_approval(
        self, request_id: str, *, decision: str, reason: str = ""
    ) -> dict[str, Any] | None:
        """CAS: only a pending approval can be resolved, exactly once."""

        now = time.time()
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE approvals
                SET status=?, decision=?, decision_reason=?, updated_at=?, resolved_at=?
                WHERE request_id=? AND status=?
                """,
                (STATUS_RESOLVED, decision, reason, now, now, str(request_id), STATUS_PENDING),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                return None
        return self.get_approval(request_id)

    # ── turn resumes (Autoloop graft item 1) ─────────────────────────
    def create_turn_resume(self, record: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        row: dict[str, Any] = {
            "resume_id": str(record["resume_id"]),
            "bot_id": str(record.get("bot_id") or ""),
            "session_id": str(record.get("session_id") or ""),
            "task_id": str(record.get("task_id") or ""),
            "trace_id": str(record.get("trace_id") or ""),
            "user_id": str(record.get("user_id") or ""),
            "thread_id": str(record.get("thread_id") or ""),
            "request_ids_json": json.dumps(
                list(record.get("request_ids") or []), ensure_ascii=False
            ),
            "rev": int(record.get("rev") or 0),
            "objective_hash": str(record.get("objective_hash") or ""),
            "state": "pending",
            "created_at": float(record.get("created_at") or now),
            "claimed_at": None,
            "expires_at": float(record.get("expires_at") or now),
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO turn_resumes
                (resume_id, bot_id, session_id, task_id, trace_id, user_id,
                 thread_id, request_ids_json, rev, objective_hash, state,
                 created_at, claimed_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["resume_id"],
                    row["bot_id"],
                    row["session_id"],
                    row["task_id"],
                    row["trace_id"],
                    row["user_id"],
                    row["thread_id"],
                    row["request_ids_json"],
                    row["rev"],
                    row["objective_hash"],
                    row["state"],
                    row["created_at"],
                    row["claimed_at"],
                    row["expires_at"],
                ),
            )
            self._conn.commit()
        return self.get_turn_resume(row["resume_id"]) or row

    def get_turn_resume(self, resume_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM turn_resumes WHERE resume_id=?", (str(resume_id),)
            ).fetchone()
        return self._to_turn_resume(row) if row is not None else None

    def claim_turn_resume(self, resume_id: str) -> dict[str, Any] | None:
        """Claim exactly once: only a pending row can become claimed."""

        now = time.time()
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE turn_resumes
                SET state=?, claimed_at=?
                WHERE resume_id=? AND state=?
                """,
                ("claimed", now, str(resume_id), "pending"),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                return None
        return self.get_turn_resume(resume_id)

    def transition_turn_resume(
        self, resume_id: str, to_state: str, *, from_states: tuple[str, ...] = ("pending", "claimed")
    ) -> dict[str, Any] | None:
        """Move a resume to a terminal state (done/expired), once only."""

        placeholders = ",".join("?" for _ in from_states)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE turn_resumes SET state=? WHERE resume_id=? AND state IN ({placeholders})",
                (to_state, str(resume_id), *from_states),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                return None
        return self.get_turn_resume(resume_id)

    def release_turn_resume(self, resume_id: str) -> dict[str, Any] | None:
        """Give a claim back: claimed -> pending (re-evaluation found it waiting)."""

        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE turn_resumes
                SET state=?, claimed_at=NULL
                WHERE resume_id=? AND state=?
                """,
                ("pending", str(resume_id), "claimed"),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                return None
        return self.get_turn_resume(resume_id)

    def expire_overdue_turn_resumes(self, now: float | None = None) -> int:
        """Mark overdue pending rows expired. Returns how many expired."""

        now = time.time() if now is None else float(now)
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE turn_resumes
                SET state=?
                WHERE state IN ('pending', 'claimed') AND expires_at<=?
                """,
                ("expired", now),
            )
            self._conn.commit()
            return int(cursor.rowcount)

    def list_turn_resumes(
        self, *, state: str = "", session_id: str = "", limit: int = 200
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM turn_resumes WHERE 1=1"
        params: list[str] = []
        if state:
            query += " AND state=?"
            params.append(state)
        if session_id:
            query += " AND session_id=?"
            params.append(session_id)
        query += f" ORDER BY created_at ASC LIMIT {max(1, min(int(limit), 500))}"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._to_turn_resume(row) for row in rows if row is not None]

    @staticmethod
    def _to_turn_resume(row: sqlite3.Row) -> dict[str, Any]:
        try:
            request_ids = json.loads(row["request_ids_json"] or "[]")
        except (json.JSONDecodeError, TypeError, ValueError):
            request_ids = []
        return {
            "resume_id": row["resume_id"],
            "bot_id": row["bot_id"],
            "session_id": row["session_id"],
            "task_id": row["task_id"],
            "trace_id": row["trace_id"],
            "user_id": row["user_id"],
            "thread_id": row["thread_id"],
            "request_ids": list(request_ids) if isinstance(request_ids, list) else [],
            "rev": row["rev"],
            "objective_hash": row["objective_hash"],
            "state": row["state"],
            "created_at": row["created_at"],
            "claimed_at": row["claimed_at"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _to_approval(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "request_id": row["request_id"],
            "scope": row["scope"],
            "tool": row["tool"],
            "tool_args": json.loads(row["tool_args_json"] or "{}"),
            "status": row["status"],
            "decision": row["decision"],
            "decision_reason": row["decision_reason"],
            "delegate_policy": row["delegate_policy"],
            "session_id": row["session_id"],
            "actor": row["actor"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "resolved_at": row["resolved_at"],
        }

    # ── grants ───────────────────────────────────────────────────────
    def put_grant(self, *, scope: str, tool: str, scope_pattern: str = "") -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO grants (scope, tool, scope_pattern, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope, tool, scope_pattern) DO UPDATE SET created_at=excluded.created_at
                """,
                (scope, tool, scope_pattern, now),
            )
            self._conn.commit()
        return self.lookup_grant(scope=scope, tool=tool, scope_pattern=scope_pattern) or {
            "scope": scope,
            "tool": tool,
            "scope_pattern": scope_pattern,
            "created_at": now,
        }

    def lookup_grant(
        self, *, scope: str, tool: str, scope_pattern: str = ""
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM grants WHERE scope=? AND tool=? AND scope_pattern=?",
                (scope, tool, scope_pattern),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "scope": row["scope"],
            "tool": row["tool"],
            "scope_pattern": row["scope_pattern"],
            "created_at": row["created_at"],
        }

    def list_grants(self, *, scope: str = "") -> list[dict[str, Any]]:
        if scope:
            query = "SELECT * FROM grants WHERE scope=? ORDER BY created_at DESC LIMIT 200"
            params: list[str] = [scope]
        else:
            query = "SELECT * FROM grants ORDER BY created_at DESC LIMIT 200"
            params = []
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {
                "id": row["id"],
                "scope": row["scope"],
                "tool": row["tool"],
                "scope_pattern": row["scope_pattern"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def revoke_grant(self, grant_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM grants WHERE id=?", (int(grant_id),))
            self._conn.commit()
            return cursor.rowcount > 0

    # ── handoffs ─────────────────────────────────────────────────────
    def create_handoff(self, record: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        row: dict[str, Any] = {
            "id": str(record["id"]),
            "scope": str(record.get("scope") or ""),
            "reason": str(record.get("reason") or ""),
            "status": HANDOFF_PENDING,
            "created_at": float(record.get("created_at") or now),
            "updated_at": now,
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO handoffs (id, scope, reason, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["scope"],
                    row["reason"],
                    row["status"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
            self._conn.commit()
        return self.get_handoff(row["id"]) or row

    def get_handoff(self, handoff_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM handoffs WHERE id=?", (str(handoff_id),)
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "scope": row["scope"],
            "reason": row["reason"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_handoffs(self, *, status: str = "") -> list[dict[str, Any]]:
        if status:
            query = "SELECT * FROM handoffs WHERE status=? ORDER BY created_at DESC LIMIT 200"
            params: list[str] = [status]
        else:
            query = "SELECT * FROM handoffs ORDER BY created_at DESC LIMIT 200"
            params = []
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {
                "id": row["id"],
                "scope": row["scope"],
                "reason": row["reason"],
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def resolve_handoff(self, handoff_id: str, *, status: str) -> dict[str, Any] | None:
        now = time.time()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE handoffs SET status=?, updated_at=? WHERE id=? AND status=?",
                (status, now, str(handoff_id), HANDOFF_PENDING),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                return None
        return self.get_handoff(handoff_id)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = [
    "DECISION_ALWAYS",
    "DECISION_DENY",
    "DECISION_ONCE",
    "DECISION_SESSION",
    "HANDOFF_CANCELLED",
    "HANDOFF_DONE",
    "HANDOFF_PENDING",
    "STATUS_PENDING",
    "STATUS_RESOLVED",
    "ApprovalStore",
    "default_approvals_path",
]
