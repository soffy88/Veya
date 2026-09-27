import asyncio
import dataclasses
import json
import time
from typing import Any, cast

from runtime.execution.runtime import get_durable_runtime


@dataclasses.dataclass
class SessionGovernanceState:
    session_id: str
    mode: str
    require_approval: bool
    freeze_root: str | None
    freeze_allow: str | None
    revision: int
    updated_at: float


@dataclasses.dataclass
class ApprovalRecord:
    request_id: str
    session_id: str
    tool: str
    tool_args: dict[str, Any]
    status: str
    decision_reason: str | None
    request_hash: str | None
    created_at: float
    updated_at: float


class GovernanceStore:
    def __init__(self):
        self._runtime = get_durable_runtime()

    async def _ensure_started(self):
        if not getattr(self, "_migrated", False):
            if not self._runtime._started:
                if self._runtime.config.enabled:
                    await self._runtime.start()
                else:
                    await self._runtime.repository.connect()
                    await self._runtime.repository.migrate()
            self._migrated = True
            await self._runtime.start()

    async def get_state(self, session_id: str) -> SessionGovernanceState | None:
        await self._ensure_started()
        repo = self._runtime.repository

        def op(conn):
            row = conn.execute(
                "SELECT * FROM session_governance WHERE session_id=?", (session_id,)
            ).fetchone()
            if row:
                return SessionGovernanceState(
                    session_id=row["session_id"],
                    mode=row["mode"],
                    require_approval=bool(row["require_approval"]),
                    freeze_root=row["freeze_root"],
                    freeze_allow=row["freeze_allow"],
                    revision=row["revision"],
                    updated_at=row["updated_at"],
                )
            return None

        if repo.backend == "sqlite":
            return cast(
                SessionGovernanceState | None,
                await asyncio.to_thread(lambda: repo._sqlite_read(op)),
            )

        async def op_pg(conn):
            row = await conn.fetchrow(
                "SELECT * FROM session_governance WHERE session_id=$1", session_id
            )
            if row:
                return SessionGovernanceState(
                    session_id=row["session_id"],
                    mode=row["mode"],
                    require_approval=bool(row["require_approval"]),
                    freeze_root=row["freeze_root"],
                    freeze_allow=row["freeze_allow"],
                    revision=row["revision"],
                    updated_at=row["updated_at"],
                )
            return None

        return cast(SessionGovernanceState | None, await repo._pg_tx(op_pg))

    async def set_state(self, state: SessionGovernanceState) -> bool:
        await self._ensure_started()
        repo = self._runtime.repository
        now = time.time()

        def op(conn):
            # CAS update
            if state.revision == 0:
                try:
                    conn.execute(
                        "INSERT INTO session_governance (session_id, mode, require_approval, freeze_root, freeze_allow, revision, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
                        (
                            state.session_id,
                            state.mode,
                            1 if state.require_approval else 0,
                            state.freeze_root,
                            state.freeze_allow,
                            now,
                        ),
                    )
                    return True
                except Exception:
                    return False
            else:
                cursor = conn.execute(
                    "UPDATE session_governance SET mode=?, require_approval=?, freeze_root=?, freeze_allow=?, revision=revision+1, updated_at=? WHERE session_id=? AND revision=?",
                    (
                        state.mode,
                        1 if state.require_approval else 0,
                        state.freeze_root,
                        state.freeze_allow,
                        now,
                        state.session_id,
                        state.revision,
                    ),
                )
                return cursor.rowcount > 0

        if repo.backend == "sqlite":
            return cast(bool, await asyncio.to_thread(repo._sqlite_tx, op))

        async def op_pg(conn):
            if state.revision == 0:
                try:
                    await conn.execute(
                        "INSERT INTO session_governance (session_id, mode, require_approval, freeze_root, freeze_allow, revision, updated_at) VALUES ($1, $2, $3, $4, $5, 1, $6)",
                        state.session_id,
                        state.mode,
                        1 if state.require_approval else 0,
                        state.freeze_root,
                        state.freeze_allow,
                        now,
                    )
                    return True
                except Exception:
                    return False
            else:
                status = await conn.execute(
                    "UPDATE session_governance SET mode=$1, require_approval=$2, freeze_root=$3, freeze_allow=$4, revision=revision+1, updated_at=$5 WHERE session_id=$6 AND revision=$7",
                    state.mode,
                    1 if state.require_approval else 0,
                    state.freeze_root,
                    state.freeze_allow,
                    now,
                    state.session_id,
                    state.revision,
                )
                return status.endswith(" 1") or status == "UPDATE 1"

        return cast(bool, await repo._pg_tx(op_pg))

    async def init_state(
        self, session_id: str, mode: str, require_approval: bool
    ) -> SessionGovernanceState:
        state = await self.get_state(session_id)
        if not state:
            state = SessionGovernanceState(
                session_id=session_id,
                mode=mode,
                require_approval=require_approval,
                freeze_root=None,
                freeze_allow=None,
                revision=0,
                updated_at=time.time(),
            )
            await self.set_state(state)
            return (await self.get_state(session_id)) or state
        return state

    async def create_approval(self, record: ApprovalRecord) -> None:
        await self._ensure_started()
        repo = self._runtime.repository

        def op(conn):
            conn.execute(
                "INSERT INTO approval_records (request_id, session_id, tool, tool_args_json, status, decision_reason, request_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.request_id,
                    record.session_id,
                    record.tool,
                    json.dumps(record.tool_args),
                    record.status,
                    record.decision_reason,
                    record.request_hash,
                    record.created_at,
                    record.updated_at,
                ),
            )

        if repo.backend == "sqlite":
            return cast(None, await asyncio.to_thread(repo._sqlite_tx, op))

        async def op_pg(conn):
            await conn.execute(
                "INSERT INTO approval_records (request_id, session_id, tool, tool_args_json, status, decision_reason, request_hash, created_at, updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                record.request_id,
                record.session_id,
                record.tool,
                json.dumps(record.tool_args),
                record.status,
                record.decision_reason,
                record.request_hash,
                record.created_at,
                record.updated_at,
            )

        return cast(None, await repo._pg_tx(op_pg))

    async def get_approval(self, request_id: str) -> ApprovalRecord | None:
        await self._ensure_started()
        repo = self._runtime.repository

        def op(conn):
            row = conn.execute(
                "SELECT * FROM approval_records WHERE request_id=?", (request_id,)
            ).fetchone()
            if row:
                return ApprovalRecord(
                    request_id=row["request_id"],
                    session_id=row["session_id"],
                    tool=row["tool"],
                    tool_args=json.loads(row["tool_args_json"]),
                    status=row["status"],
                    decision_reason=row["decision_reason"],
                    request_hash=row["request_hash"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            return None

        if repo.backend == "sqlite":
            return cast(
                ApprovalRecord | None, await asyncio.to_thread(lambda: repo._sqlite_read(op))
            )

        async def op_pg(conn):
            row = await conn.fetchrow(
                "SELECT * FROM approval_records WHERE request_id=$1", request_id
            )
            if row:
                return ApprovalRecord(
                    request_id=row["request_id"],
                    session_id=row["session_id"],
                    tool=row["tool"],
                    tool_args=json.loads(row["tool_args_json"]),
                    status=row["status"],
                    decision_reason=row["decision_reason"],
                    request_hash=row["request_hash"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            return None

        return cast(ApprovalRecord | None, await repo._pg_tx(op_pg))

    async def get_approval_by_hash(
        self, session_id: str, request_hash: str
    ) -> ApprovalRecord | None:
        await self._ensure_started()
        repo = self._runtime.repository

        def op(conn):
            row = conn.execute(
                "SELECT * FROM approval_records WHERE session_id=? AND request_hash=? ORDER BY created_at DESC LIMIT 1",
                (session_id, request_hash),
            ).fetchone()
            if row:
                return ApprovalRecord(
                    request_id=row["request_id"],
                    session_id=row["session_id"],
                    tool=row["tool"],
                    tool_args=json.loads(row["tool_args_json"]),
                    status=row["status"],
                    decision_reason=row["decision_reason"],
                    request_hash=row["request_hash"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            return None

        if repo.backend == "sqlite":
            return cast(
                ApprovalRecord | None, await asyncio.to_thread(lambda: repo._sqlite_read(op))
            )

        async def op_pg(conn):
            row = await conn.fetchrow(
                "SELECT * FROM approval_records WHERE session_id=$1 AND request_hash=$2 ORDER BY created_at DESC LIMIT 1",
                session_id,
                request_hash,
            )
            if row:
                return ApprovalRecord(
                    request_id=row["request_id"],
                    session_id=row["session_id"],
                    tool=row["tool"],
                    tool_args=json.loads(row["tool_args_json"]),
                    status=row["status"],
                    decision_reason=row["decision_reason"],
                    request_hash=row["request_hash"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            return None

        return cast(ApprovalRecord | None, await repo._pg_tx(op_pg))

    async def resolve_approval(
        self, request_id: str, status: str, reason: str | None = None
    ) -> bool:
        await self._ensure_started()
        repo = self._runtime.repository
        now = time.time()

        def op(conn):
            cursor = conn.execute(
                "UPDATE approval_records SET status=?, decision_reason=?, updated_at=? WHERE request_id=? AND status='pending'",
                (status, reason, now, request_id),
            )
            return cursor.rowcount > 0

        if repo.backend == "sqlite":
            return cast(bool, await asyncio.to_thread(repo._sqlite_tx, op))

        async def op_pg(conn):
            status_res = await conn.execute(
                "UPDATE approval_records SET status=$1, decision_reason=$2, updated_at=$3 WHERE request_id=$4 AND status='pending'",
                status,
                reason,
                now,
                request_id,
            )
            return status_res.endswith(" 1") or status_res == "UPDATE 1"

        return cast(bool, await repo._pg_tx(op_pg))


governance_store = GovernanceStore()
