"""P1-A Persistent Computer: SQLite-backed metadata store."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.bot_scope import DEFAULT_BOT_ID, require_same_bot
from runtime.computer.models import (
    CheckpointRef,
    ComputerLifecycleState,
    ComputerSession,
    CredentialRef,
    PersistentComputer,
    generate_computer_id,
)


class PersistentComputerStore:
    """SQLite-backed store for PersistentComputer metadata.

    Provides:
    - CRUD operations for PersistentComputer
    - Session tracking
    - Credential reference management
    - Checkpoint references
    - GoalRun correlation
    - Thread-safe operations
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS persistent_computers (
        computer_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        workspace_ref TEXT NOT NULL,
        browser_profile_ref TEXT,
        downloads_ref TEXT,
        credential_refs TEXT NOT NULL DEFAULT '[]',
        goal_run_refs TEXT NOT NULL DEFAULT '[]',
        checkpoint_ref TEXT,
        lifecycle_state TEXT NOT NULL DEFAULT 'created',
        created_at TEXT NOT NULL,
        last_active_at TEXT NOT NULL,
        version TEXT NOT NULL DEFAULT '1.0',
        bot_id TEXT NOT NULL DEFAULT 'veya-default',
        hash TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS computer_sessions (
        session_id TEXT PRIMARY KEY,
        computer_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        supervisor_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'active',
        metadata TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY (computer_id) REFERENCES persistent_computers(computer_id)
    );

    CREATE TABLE IF NOT EXISTS goal_run_computers (
        goal_run_id TEXT PRIMARY KEY,
        computer_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (computer_id) REFERENCES persistent_computers(computer_id)
    );

    CREATE INDEX IF NOT EXISTS idx_computers_owner ON persistent_computers(owner_id);
    CREATE INDEX IF NOT EXISTS idx_sessions_computer ON computer_sessions(computer_id);
    CREATE INDEX IF NOT EXISTS idx_goalrun_computer ON goal_run_computers(computer_id);
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(self.SCHEMA)
            # P3-A upgrade: databases created before bot isolation lack bot_id.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(persistent_computers)")}
            if "bot_id" not in columns:
                conn.execute(
                    "ALTER TABLE persistent_computers ADD COLUMN bot_id TEXT NOT NULL DEFAULT 'veya-default'"
                )
            conn.commit()
            conn.close()

    @contextmanager
    def _conn(self):
        """Thread-local connection context manager."""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _row_to_computer(self, row: sqlite3.Row) -> PersistentComputer:
        data = dict(row)
        # Parse JSON fields
        data["credential_refs"] = json.loads(data["credential_refs"] or "[]")
        data["goal_run_refs"] = json.loads(data["goal_run_refs"] or "[]")
        if data["checkpoint_ref"]:
            data["checkpoint_ref"] = json.loads(data["checkpoint_ref"])
        # P3-A: databases created before bot isolation have no bot_id column.
        data.setdefault("bot_id", DEFAULT_BOT_ID)
        return PersistentComputer.from_dict(data)

    def _compute_hash(self, computer: PersistentComputer) -> str:
        return computer.compute_hash()

    def create_computer(
        self,
        owner_id: str,
        workspace_ref: str,
        browser_profile_ref: str | None = None,
        downloads_ref: str | None = None,
        credentials: list[CredentialRef] | None = None,
        computer_id: str | None = None,
        bot_id: str = DEFAULT_BOT_ID,
    ) -> PersistentComputer:
        """Create a new persistent computer (idempotent).

        P3-A: an existing computer owned by another bot is never returned —
        reusing its id under a different bot is refused fail-closed.
        """
        if computer_id is None:
            computer_id = generate_computer_id(owner_id, workspace_ref)

        # Check if computer already exists
        existing = self.get_computer(computer_id)
        if existing:
            require_same_bot(bot_id, existing.bot_id, f"computer:{computer_id}")
            return existing

        now = datetime.now(UTC).isoformat()
        computer = PersistentComputer(
            computer_id=computer_id,
            owner_id=owner_id,
            workspace_ref=workspace_ref,
            browser_profile_ref=browser_profile_ref,
            downloads_ref=downloads_ref,
            credential_refs=credentials or [],
            lifecycle_state="created",
            created_at=now,
            last_active_at=now,
            bot_id=bot_id,
        )
        computer_hash = self._compute_hash(computer)

        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO persistent_computers
                (computer_id, owner_id, workspace_ref, browser_profile_ref, downloads_ref,
                 credential_refs, goal_run_refs, checkpoint_ref, lifecycle_state,
                 created_at, last_active_at, version, bot_id, hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    computer.computer_id,
                    computer.owner_id,
                    computer.workspace_ref,
                    computer.browser_profile_ref,
                    computer.downloads_ref,
                    json.dumps([c.to_dict() for c in computer.credential_refs]),
                    json.dumps(computer.goal_run_refs),
                    json.dumps(computer.checkpoint_ref.to_dict() if computer.checkpoint_ref else None),
                    computer.lifecycle_state,
                    computer.created_at,
                    computer.last_active_at,
                    computer.version,
                    computer.bot_id,
                    computer_hash,
                ),
            )
        return computer

    def get_computer(
        self, computer_id: str, *, bot_id: str | None = None
    ) -> PersistentComputer | None:
        """Get a computer by ID.

        P3-A: when ``bot_id`` is given, a computer owned by another bot is
        refused fail-closed instead of returned.
        """
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM persistent_computers WHERE computer_id = ?",
                (computer_id,),
            ).fetchone()
            if row:
                computer = self._row_to_computer(row)
                if bot_id is not None:
                    require_same_bot(bot_id, computer.bot_id, f"computer:{computer_id}")
                return computer
            return None

    def get_computer_by_owner_workspace(self, owner_id: str, workspace_ref: str) -> PersistentComputer | None:
        """Get a computer by owner and workspace (for idempotent create)."""
        computer_id = generate_computer_id(owner_id, workspace_ref)
        return self.get_computer(computer_id)

    def update_computer(self, computer: PersistentComputer) -> PersistentComputer:
        """Update a computer's metadata."""
        computer_hash = self._compute_hash(computer)
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                UPDATE persistent_computers SET
                    owner_id = ?, workspace_ref = ?, browser_profile_ref = ?,
                    downloads_ref = ?, credential_refs = ?, goal_run_refs = ?,
                    checkpoint_ref = ?, lifecycle_state = ?, last_active_at = ?,
                    version = ?, bot_id = ?, hash = ?
                WHERE computer_id = ?
                """,
                (
                    computer.owner_id,
                    computer.workspace_ref,
                    computer.browser_profile_ref,
                    computer.downloads_ref,
                    json.dumps([c.to_dict() for c in computer.credential_refs]),
                    json.dumps(computer.goal_run_refs),
                    json.dumps(computer.checkpoint_ref.to_dict() if computer.checkpoint_ref else None),
                    computer.lifecycle_state,
                    computer.last_active_at,
                    computer.version,
                    computer.bot_id,
                    computer_hash,
                    computer.computer_id,
                ),
            )
        return computer

    def delete_computer(self, computer_id: str) -> bool:
        """Delete a computer (only if no active sessions)."""
        with self._lock, self._conn() as conn:
            # Check for active sessions
            session = conn.execute(
                "SELECT session_id FROM computer_sessions WHERE computer_id = ? AND state = 'active'",
                (computer_id,),
            ).fetchone()
            if session:
                return False

            cursor = conn.execute(
                "DELETE FROM persistent_computers WHERE computer_id = ?",
                (computer_id,),
            )
            conn.execute(
                "DELETE FROM goal_run_computers WHERE computer_id = ?",
                (computer_id,),
            )
            return bool(cursor.rowcount > 0)

    def list_computers(self, owner_id: str | None = None, state: ComputerLifecycleState | None = None, bot_id: str | None = None) -> list[PersistentComputer]:
        """List computers with optional filters."""
        query = "SELECT * FROM persistent_computers WHERE 1=1"
        params: list[Any] = []
        if owner_id:
            query += " AND owner_id = ?"
            params.append(owner_id)
        if state:
            query += " AND lifecycle_state = ?"
            params.append(state)
        if bot_id:
            query += " AND bot_id = ?"
            params.append(bot_id)
        query += " ORDER BY last_active_at DESC"

        with self._lock, self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_computer(row) for row in rows]

    # Session management
    def create_session(
        self,
        computer_id: str,
        owner_id: str,
        supervisor_id: str,
        session_id: str | None = None,
    ) -> ComputerSession:
        """Create a new session for a computer."""
        if session_id is None:
            import uuid
            session_id = f"sess-{uuid.uuid4().hex[:24]}"

        now = datetime.now(UTC).isoformat()
        session = ComputerSession(
            session_id=session_id,
            computer_id=computer_id,
            owner_id=owner_id,
            supervisor_id=supervisor_id,
            started_at=now,
            state="active",
        )

        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO computer_sessions
                (session_id, computer_id, owner_id, supervisor_id, started_at, state, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.computer_id,
                    session.owner_id,
                    session.supervisor_id,
                    session.started_at,
                    session.state,
                    json.dumps(session.metadata),
                ),
            )
            # Update computer state
            conn.execute(
                "UPDATE persistent_computers SET lifecycle_state = 'running', last_active_at = ? WHERE computer_id = ?",
                (now, computer_id),
            )
        return session

    def get_session(self, session_id: str) -> ComputerSession | None:
        """Get a session by ID."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM computer_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row:
                return ComputerSession.from_dict(dict(row))
            return None

    def get_active_session(self, computer_id: str) -> ComputerSession | None:
        """Get the active session for a computer."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM computer_sessions WHERE computer_id = ? AND state = 'active' ORDER BY started_at DESC LIMIT 1",
                (computer_id,),
            ).fetchone()
            if row:
                return ComputerSession.from_dict(dict(row))
            return None

    def end_session(self, session_id: str) -> bool:
        """End a session."""
        with self._lock, self._conn() as conn:
            cursor = conn.execute(
                "UPDATE computer_sessions SET state = 'terminated' WHERE session_id = ?",
                (session_id,),
            )
            return bool(cursor.rowcount > 0)

    # GoalRun correlation
    def link_goal_run(self, goal_run_id: str, computer_id: str, owner_id: str, bot_id: str | None = None) -> bool:
        """Link a GoalRun to a computer.

        P3-A: when ``bot_id`` is given, linking a computer owned by another
        bot is refused (returns False, no partial link).
        """
        with self._lock, self._conn() as conn:
            # Get computer within the same connection
            row = conn.execute(
                "SELECT * FROM persistent_computers WHERE computer_id = ?",
                (computer_id,),
            ).fetchone()
            if not row:
                return False
            computer = self._row_to_computer(row)
            if computer.owner_id != owner_id:
                return False
            if bot_id is not None and computer.bot_id != bot_id:
                return False

            now = datetime.now(UTC).isoformat()
            conn.execute(
                """
                INSERT OR REPLACE INTO goal_run_computers
                (goal_run_id, computer_id, owner_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (goal_run_id, computer_id, owner_id, now),
            )
            # Update computer's goal_run_refs
            updated = computer.add_goal_run(goal_run_id)
            computer_hash = self._compute_hash(updated)
            conn.execute(
                """
                UPDATE persistent_computers SET
                    goal_run_refs = ?, last_active_at = ?, hash = ?
                WHERE computer_id = ?
                """,
                (json.dumps(updated.goal_run_refs), now, computer_hash, computer_id),
            )
        return True

    def unlink_goal_run(self, goal_run_id: str) -> bool:
        """Unlink a GoalRun from its computer."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT computer_id FROM goal_run_computers WHERE goal_run_id = ?",
                (goal_run_id,),
            ).fetchone()
            if not row:
                return False
            computer_id = row["computer_id"]
            conn.execute(
                "DELETE FROM goal_run_computers WHERE goal_run_id = ?",
                (goal_run_id,),
            )
            # Update computer's goal_run_refs
            row = conn.execute(
                "SELECT * FROM persistent_computers WHERE computer_id = ?",
                (computer_id,),
            ).fetchone()
            if row:
                computer = self._row_to_computer(row)
                updated = computer.remove_goal_run(goal_run_id)
                computer_hash = self._compute_hash(updated)
                now = datetime.now(UTC).isoformat()
                conn.execute(
                    """
                    UPDATE persistent_computers SET
                        goal_run_refs = ?, last_active_at = ?, hash = ?
                    WHERE computer_id = ?
                    """,
                    (json.dumps(updated.goal_run_refs), now, computer_hash, computer_id),
                )
        return True

    def get_computer_for_goal_run(self, goal_run_id: str, *, bot_id: str | None = None) -> PersistentComputer | None:
        """Get the computer associated with a GoalRun.

        P3-A: when ``bot_id`` is given, a computer owned by another bot is
        refused fail-closed instead of returned.
        """
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT computer_id FROM goal_run_computers WHERE goal_run_id = ?",
                (goal_run_id,),
            ).fetchone()
            if row:
                return self.get_computer(row["computer_id"], bot_id=bot_id)
            return None

    def list_goal_runs_for_computer(self, computer_id: str) -> list[str]:
        """List all GoalRun IDs linked to a computer."""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT goal_run_id FROM goal_run_computers WHERE computer_id = ?",
                (computer_id,),
            ).fetchall()
            return [row["goal_run_id"] for row in rows]

    # Credential management
    def add_credential(self, computer_id: str, credential: CredentialRef) -> PersistentComputer | None:
        """Add a credential reference to a computer."""
        computer = self.get_computer(computer_id)
        if not computer:
            return None
        updated = computer.add_credential(credential)
        return self.update_computer(updated)

    def remove_credential(self, computer_id: str, ref_id: str) -> PersistentComputer | None:
        """Remove a credential reference from a computer."""
        computer = self.get_computer(computer_id)
        if not computer:
            return None
        updated = computer.remove_credential(ref_id)
        return self.update_computer(updated)

    # Checkpoint management
    def set_checkpoint(self, computer_id: str, checkpoint: CheckpointRef) -> PersistentComputer | None:
        """Set a checkpoint for a computer.

        P3-A: a checkpoint owned by another bot is never attached.
        """
        computer = self.get_computer(computer_id)
        if not computer:
            return None
        require_same_bot(
            checkpoint.bot_id, computer.bot_id, f"checkpoint:{checkpoint.checkpoint_id}"
        )
        updated = computer.with_checkpoint(checkpoint)
        return self.update_computer(updated)

    def get_checkpoint(self, computer_id: str, *, bot_id: str | None = None) -> CheckpointRef | None:
        """Get the latest checkpoint for a computer.

        P3-A: when ``bot_id`` is given, a checkpoint owned by another bot is
        refused fail-closed instead of returned.
        """
        computer = self.get_computer(computer_id, bot_id=bot_id)
        if computer:
            checkpoint = computer.checkpoint_ref
            if checkpoint is not None and bot_id is not None:
                require_same_bot(bot_id, checkpoint.bot_id, f"checkpoint:{computer_id}")
            return checkpoint
        return None

    # State transitions
    def set_state(self, computer_id: str, state: ComputerLifecycleState) -> PersistentComputer | None:
        """Set the lifecycle state of a computer."""
        computer = self.get_computer(computer_id)
        if not computer:
            return None
        updated = computer.with_state(state)
        return self.update_computer(updated)

    def touch(self, computer_id: str) -> PersistentComputer | None:
        """Update last_active_at timestamp."""
        computer = self.get_computer(computer_id)
        if not computer:
            return None
        updated = PersistentComputer(
            computer_id=computer.computer_id,
            owner_id=computer.owner_id,
            workspace_ref=computer.workspace_ref,
            browser_profile_ref=computer.browser_profile_ref,
            downloads_ref=computer.downloads_ref,
            credential_refs=computer.credential_refs,
            goal_run_refs=computer.goal_run_refs,
            checkpoint_ref=computer.checkpoint_ref,
            lifecycle_state=computer.lifecycle_state,
            created_at=computer.created_at,
            last_active_at=datetime.now(UTC).isoformat(),
            version=computer.version,
            bot_id=computer.bot_id,
        )
        return self.update_computer(updated)


__all__ = [
    "PersistentComputerStore",
]
