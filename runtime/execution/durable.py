"""Crash-safe, cross-process execution repository.

This module is deliberately below the semantic layer.  It persists execution
facts and enforces ownership; it never decides what a user's request means.
SQLite is supported for local tests and development.  Production deployments
must use the PostgreSQL DSN path, which uses short transactions and
``FOR UPDATE SKIP LOCKED`` for claims.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schema import POSTGRES_SCHEMA, POSTGRES_UPGRADES, SCHEMA_VERSION, SQLITE_SCHEMA

TERMINAL_WORK_STATES = frozenset({"succeeded", "failed", "cancelled", "quarantined_unknown"})
TERMINAL_GOAL_STATES = frozenset({"completed", "partial_completed", "failed", "cancelled", "blocked"})
RETRYABLE_DECISIONS = frozenset({"RETRY_SAFE", "IDEMPOTENT_RETRY"})
VALID_SIDE_EFFECT_POLICIES = frozenset({"none", "idempotent", "probe_required", "manual_on_unknown"})


def _env_flag(name: str) -> bool:
    value = os.environ.get(name)
    return value is not None and value.strip().lower() not in {"", "0", "false", "off", "no"}


def new_id() -> str:
    """Generate a UUIDv7-shaped, time-sortable identifier on Python 3.11+."""
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    random_bits = uuid.uuid4().int & ((1 << 76) - 1)
    value = (timestamp_ms << 80) | (0x7 << 76) | random_bits
    value &= ~(0b11 << 62)
    value |= 0b10 << 62
    return str(uuid.UUID(int=value))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_operation_key(
    root_run_id: str,
    logical_work_item_id: str,
    operation_name: str,
    operation_version: str = "1",
    item_id: str | None = None,
) -> str:
    parts = ["veya", root_run_id, logical_work_item_id, operation_name]
    if item_id:
        parts.append(item_id)
    parts.append(operation_version)
    return ":".join(parts)


class DurableExecutionError(RuntimeError):
    """Stable machine-readable repository error."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


@dataclass
class WorkItemSpec:
    goal_run_id: str
    logical_key: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    parallel: bool = False
    priority: int = 100
    idempotency_key: str | None = None
    side_effect_policy: str = "none"
    max_attempts: int = 1
    parent_work_item_id: str | None = None
    input_manifest_id: str | None = None
    work_item_id: str | None = None

    @classmethod
    def from_value(cls, value: WorkItemSpec | dict[str, Any]) -> WorkItemSpec:
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise TypeError("work item spec must be a mapping")
        return cls(
            goal_run_id=str(value["goal_run_id"]),
            logical_key=str(value["logical_key"]),
            kind=str(value.get("kind") or "delegate"),
            payload=dict(value.get("payload") or value.get("payload_json") or {}),
            depends_on=[str(item) for item in value.get("depends_on") or value.get("dependencies") or []],
            parallel=bool(value.get("parallel", False) or value.get("parallel_intent") == "parallel_declared"),
            priority=int(value.get("priority", 100)),
            idempotency_key=value.get("idempotency_key"),
            side_effect_policy=str(value.get("side_effect_policy") or "none"),
            max_attempts=max(1, int(value.get("max_attempts", 1))),
            parent_work_item_id=value.get("parent_work_item_id"),
            input_manifest_id=value.get("input_manifest_id"),
            work_item_id=value.get("work_item_id"),
        )


@dataclass
class ClaimEnvelope:
    work_item_id: str
    goal_run_id: str
    attempt_id: str
    attempt_no: int
    worker_id: str
    lease_token: int
    lease_expires_at: float
    input_manifest_id: str | None
    input_hash: str
    side_effect_policy: str
    runtime_policy_version: str = "1.0.0"
    logical_key: str = ""
    kind: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    parallel_intent: str = "serial"

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            "goal_run_id": self.goal_run_id,
            "attempt_id": self.attempt_id,
            "attempt_no": self.attempt_no,
            "worker_id": self.worker_id,
            "lease_token": self.lease_token,
            "lease_expires_at": self.lease_expires_at,
            "input_manifest_id": self.input_manifest_id,
            "input_hash": self.input_hash,
            "side_effect_policy": self.side_effect_policy,
            "runtime_policy_version": self.runtime_policy_version,
            "logical_key": self.logical_key,
            "kind": self.kind,
            "payload": self.payload,
            "parallel_intent": self.parallel_intent,
        }


@dataclass
class ReconciliationReport:
    scanned: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)
    retry_safe: int = 0
    quarantined: int = 0
    manual_review: int = 0
    completed_from_evidence: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "decisions": self.decisions,
            "retry_safe": self.retry_safe,
            "quarantined": self.quarantined,
            "manual_review": self.manual_review,
            "completed_from_evidence": self.completed_from_evidence,
        }


@dataclass
class OutboxMessage:
    event_id: str
    event: dict[str, Any]
    destination: str
    attempts: int


def _decode(value: object, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default


class DurableExecutionRepository:
    """Repository implementing the 1.0 queue/lease/attempt contract."""

    def __init__(
        self,
        *,
        dsn: str | None = None,
        sqlite_path: str | Path | None = None,
        policy_version: str = "1.0.0",
        production: bool | None = None,
    ):
        # An explicit backend argument is authoritative.  This keeps local
        # unit-test repositories isolated even when the hosting process has a
        # production DSN in its environment; the production runtime passes
        # its DSN explicitly through DurableRuntimeConfig.
        self.dsn = (
            dsn
            if dsn is not None
            else None
            if sqlite_path is not None
            else os.environ.get("VEYA_EXECUTION_DATABASE_URL") or os.environ.get("DATABASE_URL")
        )
        self.backend = "postgres" if (self.dsn or "").startswith(("postgres://", "postgresql://")) else "sqlite"
        if self.backend == "postgres" and not self.dsn:
            raise ValueError("PostgreSQL repository requires a DSN")
        production_mode = (
            _env_flag("VEYA_EXECUTION_PRODUCTION") if production is None and sqlite_path is None else bool(production)
        )
        if self.backend == "sqlite" and production_mode:
            raise DurableExecutionError("CONFIG_INVALID", "SQLite is not approved for production execution authority")
        self.sqlite_path = Path(
            sqlite_path or os.environ.get("VEYA_EXECUTION_SQLITE_PATH") or ".veya/execution-runtime.sqlite3"
        ).expanduser()
        self.policy_version = policy_version
        self._sqlite_lock = threading.RLock()
        self._sqlite_memory: Any = None
        self._pool: Any = None

    async def connect(self) -> None:
        if self.backend == "postgres":
            try:
                import asyncpg
            except ImportError as exc:  # pragma: no cover - production dependency
                raise DurableExecutionError("CONFIG_INVALID", "asyncpg is required for PostgreSQL execution") from exc
            self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10, command_timeout=10)
        else:
            await asyncio.to_thread(self._sqlite_prepare)
        await self.migrate()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def migrate(self) -> None:
        if self.backend == "postgres":
            if self._pool is None:
                raise DurableExecutionError("DATABASE_UNAVAILABLE", "repository is not connected")
            async with self._pool.acquire() as conn, conn.transaction():
                # Every backend process runs the repository bootstrap on
                # connect. Serialize DDL and make upgrades conditional so a
                # worker-start storm cannot deadlock on repeated ALTER TABLE.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    "veya:execution:schema",
                )
                for statement in POSTGRES_SCHEMA:
                    await conn.execute(statement)
                current_version = await conn.fetchval(
                    "SELECT COALESCE(MAX(version),0) FROM execution_schema_meta"
                )
                if int(current_version or 0) < SCHEMA_VERSION:
                    for statement in POSTGRES_UPGRADES:
                        await conn.execute(statement)
                await conn.execute(
                    "INSERT INTO execution_schema_meta(version, applied_at) VALUES($1,$2) ON CONFLICT(version) DO NOTHING",
                    SCHEMA_VERSION,
                    time.time(),
                )
        else:
            await asyncio.to_thread(self._sqlite_migrate)

    def _sqlite_prepare(self) -> None:
        if str(self.sqlite_path) == ":memory:":
            with self._sqlite_lock:
                import sqlite3

                if self._sqlite_memory is None:
                    self._sqlite_memory = sqlite3.connect(":memory:", check_same_thread=False)
                    self._sqlite_memory.row_factory = sqlite3.Row
                    self._sqlite_memory.execute("PRAGMA foreign_keys=ON")
        else:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    def _sqlite_connection(self):
        import sqlite3

        if str(self.sqlite_path) == ":memory:":
            return self._sqlite_memory
        conn = sqlite3.connect(str(self.sqlite_path), timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _sqlite_migrate(self) -> None:
        self._sqlite_prepare()
        with self._sqlite_lock:
            conn = self._sqlite_connection()
            try:
                for statement in SQLITE_SCHEMA:
                    conn.execute(statement)
                conn.execute(
                    "INSERT OR IGNORE INTO execution_schema_meta(version, applied_at) VALUES(?,?)",
                    (SCHEMA_VERSION, time.time()),
                )
                conn.commit()
            finally:
                if conn is not self._sqlite_memory:
                    conn.close()

    def _sqlite_tx(self, fn: Callable[[Any], Any]) -> Any:
        self._sqlite_prepare()
        with self._sqlite_lock:
            conn = self._sqlite_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = fn(conn)
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise
            finally:
                if conn is not self._sqlite_memory:
                    conn.close()

    async def _pg_tx(self, fn: Callable[[Any], Awaitable[Any]]) -> Any:
        if self._pool is None:
            raise DurableExecutionError("DATABASE_UNAVAILABLE", "repository is not connected")
        async with self._pool.acquire() as conn, conn.transaction():
            return await fn(conn)

    @staticmethod
    def _event_payload(
        *,
        event_id: str,
        aggregate_type: str,
        aggregate_id: str,
        goal_run_id: str,
        sequence_no: int,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        trace_id: str | None,
        occurred_at: float,
    ) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "event_type": event_type,
            "event_version": 1,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "goal_run_id": goal_run_id,
            "sequence_no": sequence_no,
            "occurred_at": occurred_at,
            "actor": {"type": "runtime", "id": "repository"},
            "trace_id": trace_id,
            "idempotency_key": idempotency_key,
            "payload": payload,
        }

    @classmethod
    def _sqlite_event(
        cls,
        conn: Any,
        *,
        aggregate_type: str,
        aggregate_id: str,
        goal_run_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        existing = conn.execute(
            "SELECT event_json FROM execution_events WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if existing:
            return _decode(existing[0], {})
        now = time.time()
        seq = int(
            conn.execute(
                "SELECT COALESCE(MAX(sequence_no),0)+1 FROM execution_events WHERE aggregate_type=? AND aggregate_id=?",
                (aggregate_type, aggregate_id),
            ).fetchone()[0]
        )
        event = cls._event_payload(
            event_id=new_id(),
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            goal_run_id=goal_run_id,
            sequence_no=seq,
            event_type=event_type,
            payload=payload,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            occurred_at=now,
        )
        conn.execute(
            "INSERT INTO execution_events(id,aggregate_type,aggregate_id,goal_run_id,sequence_no,event_type,event_version,event_json,idempotency_key,trace_id,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                event["event_id"], aggregate_type, aggregate_id, goal_run_id, seq, event_type, 1,
                canonical_json(event), idempotency_key, trace_id, now,
            ),
        )
        conn.execute(
            "INSERT INTO execution_outbox(event_id,destination,next_attempt_at) VALUES(?,?,?)",
            (event["event_id"], "sse", now),
        )
        return event

    async def _pg_event(
        self,
        conn: Any,
        *,
        aggregate_type: str,
        aggregate_id: str,
        goal_run_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        existing = await conn.fetchrow(
            "SELECT event_json FROM execution_events WHERE idempotency_key=$1", idempotency_key
        )
        if existing:
            return _decode(existing["event_json"], {})
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1 || ':' || $2))", aggregate_type, aggregate_id)
        existing = await conn.fetchrow(
            "SELECT event_json FROM execution_events WHERE idempotency_key=$1", idempotency_key
        )
        if existing:
            return _decode(existing["event_json"], {})
        now = time.time()
        seq = int(
            await conn.fetchval(
                "SELECT COALESCE(MAX(sequence_no),0)+1 FROM execution_events WHERE aggregate_type=$1 AND aggregate_id=$2",
                aggregate_type, aggregate_id,
            )
        )
        event = self._event_payload(
            event_id=new_id(), aggregate_type=aggregate_type, aggregate_id=aggregate_id,
            goal_run_id=goal_run_id, sequence_no=seq, event_type=event_type, payload=payload,
            idempotency_key=idempotency_key, trace_id=trace_id, occurred_at=now,
        )
        await conn.execute(
            "INSERT INTO execution_events(id,aggregate_type,aggregate_id,goal_run_id,sequence_no,event_type,event_version,event_json,idempotency_key,trace_id,occurred_at) VALUES($1,$2,$3,$4,$5,$6,1,$7,$8,$9,$10)",
            event["event_id"], aggregate_type, aggregate_id, goal_run_id, seq, event_type,
            canonical_json(event), idempotency_key, trace_id, now,
        )
        await conn.execute(
            "INSERT INTO execution_outbox(event_id,destination,next_attempt_at) VALUES($1,$2,$3)",
            event["event_id"], "sse", now,
        )
        return event

    async def _record_fence_rejection(
        self, claim: ClaimEnvelope, operation: str
    ) -> None:
        """Persist stale-owner telemetry without masking the safety rejection."""
        payload = {
            "attempt_id": claim.attempt_id,
            "owner": claim.worker_id,
            "lease_token": claim.lease_token,
            "operation": operation,
        }
        key = f"fenced-out:{claim.attempt_id}:{operation}"
        try:
            if self.backend == "sqlite":
                await asyncio.to_thread(
                    self._sqlite_tx,
                    lambda conn: self._sqlite_event(
                        conn,
                        aggregate_type="work_item",
                        aggregate_id=claim.work_item_id,
                        goal_run_id=claim.goal_run_id,
                        event_type="work_item.fenced_out",
                        payload=payload,
                        idempotency_key=key,
                    ),
                )
                return

            async def op_pg(conn: Any) -> None:
                await self._pg_event(
                    conn,
                    aggregate_type="work_item",
                    aggregate_id=claim.work_item_id,
                    goal_run_id=claim.goal_run_id,
                    event_type="work_item.fenced_out",
                    payload=payload,
                    idempotency_key=key,
                )

            await self._pg_tx(op_pg)
        except Exception:
            # Safety is fail-closed in the caller; telemetry is quality-only.
            return

    async def _guard_fenced(
        self,
        claim: ClaimEnvelope,
        operation: str,
        action: Callable[[], Awaitable[Any]],
    ) -> Any:
        try:
            return await action()
        except DurableExecutionError as exc:
            if exc.code == "STALE_FENCE":
                await self._record_fence_rejection(claim, operation)
            raise

    async def create_goal_run(
        self,
        *,
        goal_run_id: str | None = None,
        root_run_id: str | None = None,
        parent_run_id: str | None = None,
        master_agent_id: str = "master",
        status: str = "created",
        plan_version: int = 1,
        budget: dict[str, Any] | None = None,
        acceptance: list[dict[str, Any]] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        run_id = goal_run_id or new_id()
        key = idempotency_key or f"goal-run:{run_id}"
        root_id = root_run_id or run_id
        now = time.time()
        budget_json = canonical_json(budget or {})
        acceptance_json = canonical_json(acceptance or [])

        def insert(conn: Any) -> dict[str, Any]:
            row = conn.execute("SELECT * FROM goal_runs WHERE idempotency_key=?", (key,)).fetchone()
            if row:
                return dict(row)
            conn.execute(
                "INSERT INTO goal_runs(id,parent_run_id,root_run_id,master_agent_id,status,plan_version,budget_json,acceptance_json,revision,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, parent_run_id, root_id, master_agent_id, status, plan_version, budget_json, acceptance_json, 0, key, now, now),
            )
            self._sqlite_event(
                conn, aggregate_type="goal_run", aggregate_id=run_id, goal_run_id=run_id,
                event_type="goal_run.created", payload={"plan_version": plan_version, "policy_version": self.policy_version},
                idempotency_key=f"goal-run-created:{key}",
            )
            return dict(conn.execute("SELECT * FROM goal_runs WHERE id=?", (run_id,)).fetchone())

        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, insert)

        async def insert_pg(conn: Any) -> dict[str, Any]:
            row = await conn.fetchrow("SELECT * FROM goal_runs WHERE idempotency_key=$1", key)
            if row:
                return dict(row)
            await conn.execute(
                "INSERT INTO goal_runs(id,parent_run_id,root_run_id,master_agent_id,status,plan_version,budget_json,acceptance_json,revision,idempotency_key,created_at,updated_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,0,$9,$10,$10) ON CONFLICT(idempotency_key) DO NOTHING",
                run_id, parent_run_id, root_id, master_agent_id, status, plan_version, budget_json, acceptance_json, key, now,
            )
            row = await conn.fetchrow("SELECT * FROM goal_runs WHERE idempotency_key=$1", key)
            if row is None:
                raise DurableExecutionError("CREATE_FAILED", "goal run insert was not visible")
            if row["id"] != run_id:
                return dict(row)
            await self._pg_event(
                conn, aggregate_type="goal_run", aggregate_id=run_id, goal_run_id=run_id,
                event_type="goal_run.created", payload={"plan_version": plan_version, "policy_version": self.policy_version},
                idempotency_key=f"goal-run-created:{key}",
            )
            return dict(row)

        return await self._pg_tx(insert_pg)

    async def enqueue_work_item(self, spec: WorkItemSpec | dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        item = WorkItemSpec.from_value(spec)
        if item.side_effect_policy not in VALID_SIDE_EFFECT_POLICIES:
            raise DurableExecutionError("INVALID_SIDE_EFFECT_POLICY", item.side_effect_policy)
        key = idempotency_key or item.idempotency_key or f"{item.goal_run_id}:{item.logical_key}"
        work_id = item.work_item_id or new_id()
        now = time.time()
        state = "ready" if not item.depends_on else "created"
        payload_json = canonical_json(item.payload)
        dependency_json = canonical_json(item.depends_on)
        parallel_intent = "parallel_declared" if item.parallel else "serial"

        def insert(conn: Any) -> dict[str, Any]:
            goal = conn.execute("SELECT status FROM goal_runs WHERE id=?", (item.goal_run_id,)).fetchone()
            if goal is None:
                raise DurableExecutionError("NOT_FOUND", "goal run does not exist")
            if goal[0] in TERMINAL_GOAL_STATES or (goal[0] == "cancelling" and item.kind != "finalize"):
                raise DurableExecutionError("GOAL_NOT_ACCEPTING_WORK", goal[0])
            row = conn.execute("SELECT * FROM work_items WHERE idempotency_key=?", (key,)).fetchone()
            if row:
                return dict(row)
            row = conn.execute("SELECT * FROM work_items WHERE goal_run_id=? AND logical_key=?", (item.goal_run_id, item.logical_key)).fetchone()
            if row:
                return dict(row)
            conn.execute(
                "INSERT INTO work_items(id,goal_run_id,parent_work_item_id,logical_key,kind,state,priority,dependency_json,parallel_intent,payload_json,input_manifest_id,idempotency_key,side_effect_policy,attempt_count,max_attempts,next_ready_at,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (work_id, item.goal_run_id, item.parent_work_item_id, item.logical_key, item.kind, state, max(0, min(item.priority, 100000)), dependency_json, parallel_intent, payload_json, item.input_manifest_id, key, item.side_effect_policy, 0, item.max_attempts, now, 0, now, now),
            )
            self._sqlite_event(
                conn, aggregate_type="work_item", aggregate_id=work_id, goal_run_id=item.goal_run_id,
                event_type="work_item.created", payload={"logical_key": item.logical_key, "kind": item.kind, "dependencies": item.depends_on, "idempotency_key": key},
                idempotency_key=f"work-created:{key}",
            )
            if state == "ready":
                self._sqlite_event(
                    conn, aggregate_type="work_item", aggregate_id=work_id, goal_run_id=item.goal_run_id,
                    event_type="work_item.ready", payload={"reason": "no_dependencies"},
                    idempotency_key=f"work-ready:{key}",
                )
            return dict(conn.execute("SELECT * FROM work_items WHERE id=?", (work_id,)).fetchone())

        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, insert)

        async def insert_pg(conn: Any) -> dict[str, Any]:
            goal = await conn.fetchrow("SELECT status FROM goal_runs WHERE id=$1", item.goal_run_id)
            if goal is None:
                raise DurableExecutionError("NOT_FOUND", "goal run does not exist")
            if goal["status"] in TERMINAL_GOAL_STATES or (goal["status"] == "cancelling" and item.kind != "finalize"):
                raise DurableExecutionError("GOAL_NOT_ACCEPTING_WORK", goal["status"])
            row = await conn.fetchrow("SELECT * FROM work_items WHERE idempotency_key=$1", key)
            if row:
                return dict(row)
            await conn.execute(
                "INSERT INTO work_items(id,goal_run_id,parent_work_item_id,logical_key,kind,state,priority,dependency_json,parallel_intent,payload_json,input_manifest_id,idempotency_key,side_effect_policy,attempt_count,max_attempts,next_ready_at,revision,created_at,updated_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,0,$14,$15,0,$15,$15) ON CONFLICT(idempotency_key) DO NOTHING",
                work_id, item.goal_run_id, item.parent_work_item_id, item.logical_key, item.kind, state, max(0, min(item.priority, 100000)), dependency_json, parallel_intent, payload_json, item.input_manifest_id, key, item.side_effect_policy, item.max_attempts, now,
            )
            row = await conn.fetchrow("SELECT * FROM work_items WHERE idempotency_key=$1", key)
            if row is None:
                row = await conn.fetchrow("SELECT * FROM work_items WHERE goal_run_id=$1 AND logical_key=$2", item.goal_run_id, item.logical_key)
            if row is None:
                raise DurableExecutionError("ENQUEUE_FAILED", "work item insert was not visible")
            if row["id"] != work_id:
                return dict(row)
            await self._pg_event(
                conn, aggregate_type="work_item", aggregate_id=work_id, goal_run_id=item.goal_run_id,
                event_type="work_item.created", payload={"logical_key": item.logical_key, "kind": item.kind, "dependencies": item.depends_on, "idempotency_key": key},
                idempotency_key=f"work-created:{key}",
            )
            if state == "ready":
                await self._pg_event(
                    conn, aggregate_type="work_item", aggregate_id=work_id, goal_run_id=item.goal_run_id,
                    event_type="work_item.ready", payload={"reason": "no_dependencies"}, idempotency_key=f"work-ready:{key}",
                )
            return dict(row)

        return await self._pg_tx(insert_pg)

    @staticmethod
    def _dependencies_satisfied(conn: Any, row: Any) -> bool:
        dependencies = _decode(row["dependency_json"], [])
        if not dependencies:
            return True
        placeholders = ",".join("?" for _ in dependencies)
        states = conn.execute(
            f"SELECT logical_key,state FROM work_items WHERE goal_run_id=? AND logical_key IN ({placeholders})",
            (row["goal_run_id"], *dependencies),
        ).fetchall()
        by_key = {item["logical_key"]: item["state"] for item in states}
        return all(by_key.get(dep) == "succeeded" for dep in dependencies)

    async def claim_next(
        self,
        worker_id: str,
        *,
        capabilities: Iterable[str] | None = None,
        limits: dict[str, Any] | None = None,
        lease_ttl_s: float = 30.0,
        process_id: str | None = None,
        goal_run_id: str | None = None,
        kinds: Iterable[str] | None = None,
        logical_key: str | None = None,
    ) -> ClaimEnvelope | None:
        del limits  # budget admission remains a deterministic caller policy projection.
        capabilities_set = set(capabilities or ())
        kinds_set = set(kinds or ())
        process = process_id or f"{socket.gethostname()}:{os.getpid()}"
        now = time.time()

        def claim(conn: Any) -> ClaimEnvelope | None:
            clauses = ["state IN ('ready','retry_wait')", "next_ready_at <= ?", "attempt_count < max_attempts"]
            params: list[Any] = [now]
            if goal_run_id:
                clauses.append("goal_run_id=?")
                params.append(goal_run_id)
            if logical_key:
                clauses.append("logical_key=?")
                params.append(logical_key)
            if kinds_set:
                clauses.append("kind IN (" + ",".join("?" for _ in kinds_set) + ")")
                params.extend(sorted(kinds_set))
            rows = conn.execute(
                "SELECT * FROM work_items WHERE " + " AND ".join(clauses) + " ORDER BY priority ASC,next_ready_at ASC,created_at ASC LIMIT 100",
                params,
            ).fetchall()
            blocked_goals: set[str] = set()
            for row in rows:
                if row["goal_run_id"] in blocked_goals:
                    continue
                goal = conn.execute("SELECT status FROM goal_runs WHERE id=?", (row["goal_run_id"],)).fetchone()
                if goal is None or goal[0] in TERMINAL_GOAL_STATES or (goal[0] == "cancelling" and row["kind"] != "finalize"):
                    continue
                if row["kind"] not in capabilities_set and capabilities_set and "*" not in capabilities_set:
                    continue
                if not self._dependencies_satisfied(conn, row):
                    continue
                budget_row = conn.execute("SELECT budget_json FROM goal_runs WHERE id=?", (row["goal_run_id"],)).fetchone()
                budget = _decode(budget_row[0] if budget_row else None, {})
                max_parallel = max(1, int(budget.get("max_parallel", 4)))
                active_rows = conn.execute(
                    "SELECT wi.parallel_intent FROM work_items wi "
                    "JOIN execution_leases el ON el.work_item_id=wi.id "
                    "WHERE wi.goal_run_id=? AND wi.state IN ('leased','running') "
                    "AND el.released_at IS NULL AND el.expires_at>?",
                    (row["goal_run_id"], now),
                ).fetchall()
                active_count = len(active_rows)
                if any(active[0] == "serial" for active in active_rows) or (
                    row["parallel_intent"] == "serial" and active_count
                ):
                    blocked_goals.add(row["goal_run_id"])
                    continue
                if active_count >= max_parallel:
                    blocked_goals.add(row["goal_run_id"])
                    continue
                token = int(row["lease_token"] or 0) + 1
                attempt_no = int(row["attempt_count"]) + 1
                attempt_id = new_id()
                expires = now + max(1.0, lease_ttl_s)
                input_hash = content_hash(_decode(row["payload_json"], {}))
                conn.execute(
                    "UPDATE work_items SET state='leased',attempt_count=?,lease_owner=?,lease_token=?,lease_expires_at=?,last_heartbeat_at=?,revision=revision+1,updated_at=? WHERE id=? AND state IN ('ready','retry_wait') AND attempt_count < max_attempts",
                    (attempt_no, worker_id, token, expires, now, now, row["id"]),
                )
                conn.execute(
                    "INSERT INTO work_attempts(id,work_item_id,attempt_no,worker_id,process_id,lease_token,state,input_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (attempt_id, row["id"], attempt_no, worker_id, process, token, "claimed", input_hash, now),
                )
                conn.execute(
                    "INSERT INTO execution_leases(work_item_id,owner_id,token,acquired_at,heartbeat_at,expires_at,released_at,release_reason) VALUES(?,?,?,?,?,?,NULL,NULL) ON CONFLICT(work_item_id) DO UPDATE SET owner_id=excluded.owner_id,token=excluded.token,acquired_at=excluded.acquired_at,heartbeat_at=excluded.heartbeat_at,expires_at=excluded.expires_at,released_at=NULL,release_reason=NULL",
                    (row["id"], worker_id, token, now, now, expires),
                )
                self._sqlite_event(
                    conn, aggregate_type="work_item", aggregate_id=row["id"], goal_run_id=row["goal_run_id"],
                    event_type="work_item.claimed", payload={"attempt_id": attempt_id, "owner": worker_id, "lease_token": token, "lease_expires_at": expires},
                    idempotency_key=f"claimed:{attempt_id}",
                )
                return ClaimEnvelope(
                    work_item_id=row["id"], goal_run_id=row["goal_run_id"], attempt_id=attempt_id,
                    attempt_no=attempt_no, worker_id=worker_id, lease_token=token, lease_expires_at=expires,
                    input_manifest_id=row["input_manifest_id"], input_hash=input_hash,
                    side_effect_policy=row["side_effect_policy"], runtime_policy_version=self.policy_version,
                    logical_key=row["logical_key"], kind=row["kind"], payload=_decode(row["payload_json"], {}),
                    parallel_intent=row["parallel_intent"],
                )
            return None

        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, claim)

        async def claim_pg(conn: Any) -> ClaimEnvelope | None:
            clauses = ["wi.state IN ('ready','retry_wait')", "wi.next_ready_at <= $1", "wi.attempt_count < wi.max_attempts", "(gr.status NOT IN ('completed','partial_completed','failed','cancelled','blocked','cancelling') OR (gr.status='cancelling' AND wi.kind='finalize'))"]
            params: list[Any] = [now]
            if goal_run_id:
                clauses.append(f"wi.goal_run_id=${len(params)+1}")
                params.append(goal_run_id)
            if logical_key:
                clauses.append(f"wi.logical_key=${len(params)+1}")
                params.append(logical_key)
            if kinds_set:
                placeholders = ",".join(f"${len(params)+idx+1}" for idx in range(len(kinds_set)))
                clauses.append(f"wi.kind IN ({placeholders})")
                params.extend(sorted(kinds_set))
            rows = await conn.fetch(
                "SELECT wi.*,gr.status AS goal_status,gr.budget_json FROM work_items wi JOIN goal_runs gr ON gr.id=wi.goal_run_id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY wi.priority ASC,wi.next_ready_at ASC,wi.created_at ASC LIMIT 100",
                *params,
            )
            for row in rows:
                if capabilities_set and row["kind"] not in capabilities_set and "*" not in capabilities_set:
                    continue
                dependencies = _decode(row["dependency_json"], [])
                if dependencies:
                    dep_rows = await conn.fetch(
                        "SELECT logical_key,state FROM work_items WHERE goal_run_id=$1 AND logical_key=ANY($2::text[])",
                        row["goal_run_id"], list(dependencies),
                    )
                    if any({item["logical_key"]: item["state"] for item in dep_rows}.get(dep) != "succeeded" for dep in dependencies):
                        continue
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"veya:claim:{row['goal_run_id']}")
                # The candidate scan is intentionally unlocked. Lock exactly
                # one candidate after the per-goal advisory lock; locking a
                # LIMIT-100 result set serializes all workers and defeats the
                # continuous scheduler under a claim storm.
                locked = await conn.fetchrow(
                    "SELECT wi.*,gr.status AS goal_status,gr.budget_json "
                    "FROM work_items wi JOIN goal_runs gr ON gr.id=wi.goal_run_id "
                    "WHERE wi.id=$1 AND wi.state IN ('ready','retry_wait') "
                    "AND wi.next_ready_at <= $2 AND wi.attempt_count < wi.max_attempts "
                    "AND (gr.status NOT IN ('completed','partial_completed','failed','cancelled','blocked','cancelling') OR (gr.status='cancelling' AND wi.kind='finalize')) "
                    "FOR UPDATE OF wi",
                    row["id"], now,
                )
                if locked is None:
                    continue
                row = locked
                dependencies = _decode(row["dependency_json"], [])
                if dependencies:
                    dep_rows = await conn.fetch(
                        "SELECT logical_key,state FROM work_items WHERE goal_run_id=$1 AND logical_key=ANY($2::text[])",
                        row["goal_run_id"], list(dependencies),
                    )
                    if any({item["logical_key"]: item["state"] for item in dep_rows}.get(dep) != "succeeded" for dep in dependencies):
                        continue
                budget = _decode(row["budget_json"], {})
                max_parallel = max(1, int(budget.get("max_parallel", 4)))
                active_rows = await conn.fetch(
                    "SELECT wi.parallel_intent FROM work_items wi "
                    "JOIN execution_leases el ON el.work_item_id=wi.id "
                    "WHERE wi.goal_run_id=$1 AND wi.state IN ('leased','running') "
                    "AND el.released_at IS NULL AND el.expires_at>$2",
                    row["goal_run_id"], now,
                )
                active_count = len(active_rows)
                if any(active["parallel_intent"] == "serial" for active in active_rows) or (
                    row["parallel_intent"] == "serial" and active_count
                ) or active_count >= max_parallel:
                    continue
                token = int(row["lease_token"] or 0) + 1
                attempt_no = int(row["attempt_count"]) + 1
                attempt_id = new_id()
                expires = now + max(1.0, lease_ttl_s)
                input_hash = content_hash(_decode(row["payload_json"], {}))
                await conn.execute(
                    "UPDATE work_items SET state='leased',attempt_count=$1,lease_owner=$2,lease_token=$3,lease_expires_at=$4,last_heartbeat_at=$5,revision=revision+1,updated_at=$5 WHERE id=$6",
                    attempt_no, worker_id, token, expires, now, row["id"],
                )
                await conn.execute(
                    "INSERT INTO work_attempts(id,work_item_id,attempt_no,worker_id,process_id,lease_token,state,input_hash,created_at) VALUES($1,$2,$3,$4,$5,$6,'claimed',$7,$8)",
                    attempt_id, row["id"], attempt_no, worker_id, process, token, input_hash, now,
                )
                await conn.execute(
                    "INSERT INTO execution_leases(work_item_id,owner_id,token,acquired_at,heartbeat_at,expires_at,released_at,release_reason) VALUES($1,$2,$3,$4,$4,$5,NULL,NULL) ON CONFLICT(work_item_id) DO UPDATE SET owner_id=EXCLUDED.owner_id,token=EXCLUDED.token,acquired_at=EXCLUDED.acquired_at,heartbeat_at=EXCLUDED.heartbeat_at,expires_at=EXCLUDED.expires_at,released_at=NULL,release_reason=NULL",
                    row["id"], worker_id, token, now, expires,
                )
                await self._pg_event(
                    conn, aggregate_type="work_item", aggregate_id=row["id"], goal_run_id=row["goal_run_id"],
                    event_type="work_item.claimed", payload={"attempt_id": attempt_id, "owner": worker_id, "lease_token": token, "lease_expires_at": expires},
                    idempotency_key=f"claimed:{attempt_id}",
                )
                return ClaimEnvelope(
                    work_item_id=row["id"], goal_run_id=row["goal_run_id"], attempt_id=attempt_id,
                    attempt_no=attempt_no, worker_id=worker_id, lease_token=token, lease_expires_at=expires,
                    input_manifest_id=row["input_manifest_id"], input_hash=input_hash,
                    side_effect_policy=row["side_effect_policy"], runtime_policy_version=self.policy_version,
                    logical_key=row["logical_key"], kind=row["kind"], payload=_decode(row["payload_json"], {}),
                    parallel_intent=row["parallel_intent"],
                )
            return None

        return await self._pg_tx(claim_pg)

    async def start(self, claim: ClaimEnvelope) -> None:
        now = time.time()

        def op(conn: Any) -> None:
            changed = conn.execute(
                "UPDATE work_items SET state='running',revision=revision+1,updated_at=? WHERE id=? AND lease_owner=? AND lease_token=? AND state='leased' AND lease_expires_at>?",
                (now, claim.work_item_id, claim.worker_id, claim.lease_token, now),
            ).rowcount
            if changed != 1:
                raise DurableExecutionError("STALE_FENCE", "claim is no longer current")
            conn.execute("UPDATE work_attempts SET state='started',started_at=?,last_heartbeat_at=? WHERE id=? AND lease_token=?", (now, now, claim.attempt_id, claim.lease_token))
            self._sqlite_event(conn, aggregate_type="work_item", aggregate_id=claim.work_item_id, goal_run_id=claim.goal_run_id, event_type="work_item.started", payload={"attempt_id": claim.attempt_id, "input_hash": claim.input_hash}, idempotency_key=f"started:{claim.attempt_id}")

        if self.backend == "sqlite":
            await self._guard_fenced(claim, "start", lambda: asyncio.to_thread(self._sqlite_tx, op))
            return

        async def op_pg(conn: Any) -> None:
            changed = await conn.execute(
                "UPDATE work_items SET state='running',revision=revision+1,updated_at=$1 WHERE id=$2 AND lease_owner=$3 AND lease_token=$4 AND state='leased' AND lease_expires_at>$1",
                now, claim.work_item_id, claim.worker_id, claim.lease_token,
            )
            if not changed.endswith("1"):
                raise DurableExecutionError("STALE_FENCE", "claim is no longer current")
            await conn.execute("UPDATE work_attempts SET state='started',started_at=$1,last_heartbeat_at=$1 WHERE id=$2 AND lease_token=$3", now, claim.attempt_id, claim.lease_token)
            await self._pg_event(conn, aggregate_type="work_item", aggregate_id=claim.work_item_id, goal_run_id=claim.goal_run_id, event_type="work_item.started", payload={"attempt_id": claim.attempt_id, "input_hash": claim.input_hash}, idempotency_key=f"started:{claim.attempt_id}")

        await self._guard_fenced(claim, "start", lambda: self._pg_tx(op_pg))

    async def heartbeat(self, claim: ClaimEnvelope, progress: dict[str, Any] | None = None, *, lease_ttl_s: float = 30.0) -> float:
        now = time.time()
        expires = now + max(1.0, lease_ttl_s)
        progress = dict(progress or {})

        def op(conn: Any) -> float:
            changed = conn.execute(
                "UPDATE execution_leases SET heartbeat_at=?,expires_at=? WHERE work_item_id=? AND owner_id=? AND token=? AND released_at IS NULL AND expires_at>?",
                (now, expires, claim.work_item_id, claim.worker_id, claim.lease_token, now),
            ).rowcount
            if changed != 1:
                raise DurableExecutionError("STALE_FENCE", "heartbeat rejected")
            conn.execute("UPDATE work_items SET last_heartbeat_at=?,lease_expires_at=?,revision=revision+1,updated_at=? WHERE id=? AND lease_owner=? AND lease_token=?", (now, expires, now, claim.work_item_id, claim.worker_id, claim.lease_token))
            conn.execute("UPDATE work_attempts SET last_heartbeat_at=? WHERE id=? AND lease_token=?", (now, claim.attempt_id, claim.lease_token))
            self._sqlite_event(conn, aggregate_type="work_item", aggregate_id=claim.work_item_id, goal_run_id=claim.goal_run_id, event_type="work_item.heartbeat", payload={"attempt_id": claim.attempt_id, "progress": progress, "heartbeat_at": now}, idempotency_key=f"heartbeat:{claim.attempt_id}:{int(now*10)}")
            return expires

        if self.backend == "sqlite":
            return await self._guard_fenced(
                claim, "heartbeat", lambda: asyncio.to_thread(self._sqlite_tx, op)
            )

        async def op_pg(conn: Any) -> float:
            result = await conn.execute("UPDATE execution_leases SET heartbeat_at=$1,expires_at=$2 WHERE work_item_id=$3 AND owner_id=$4 AND token=$5 AND released_at IS NULL AND expires_at>$1", now, expires, claim.work_item_id, claim.worker_id, claim.lease_token)
            if not result.endswith("1"):
                raise DurableExecutionError("STALE_FENCE", "heartbeat rejected")
            await conn.execute("UPDATE work_items SET last_heartbeat_at=$1,lease_expires_at=$2,revision=revision+1,updated_at=$1 WHERE id=$3 AND lease_owner=$4 AND lease_token=$5", now, expires, claim.work_item_id, claim.worker_id, claim.lease_token)
            await conn.execute("UPDATE work_attempts SET last_heartbeat_at=$1 WHERE id=$2 AND lease_token=$3", now, claim.attempt_id, claim.lease_token)
            await self._pg_event(conn, aggregate_type="work_item", aggregate_id=claim.work_item_id, goal_run_id=claim.goal_run_id, event_type="work_item.heartbeat", payload={"attempt_id": claim.attempt_id, "progress": progress, "heartbeat_at": now}, idempotency_key=f"heartbeat:{claim.attempt_id}:{int(now*10)}")
            return expires

        return await self._guard_fenced(claim, "heartbeat", lambda: self._pg_tx(op_pg))

    async def checkpoint(self, claim: ClaimEnvelope, checkpoint: dict[str, Any]) -> str:
        checkpoint_id = str(checkpoint.get("id") or new_id())
        checkpoint_data = {**checkpoint, "id": checkpoint_id, "work_item_id": claim.work_item_id, "attempt_id": claim.attempt_id}

        def op(conn: Any) -> str:
            changed = conn.execute("UPDATE work_items SET checkpoint_id=?,revision=revision+1,updated_at=? WHERE id=? AND lease_owner=? AND lease_token=? AND state IN ('leased','running')", (checkpoint_id, time.time(), claim.work_item_id, claim.worker_id, claim.lease_token)).rowcount
            if changed != 1:
                raise DurableExecutionError("STALE_FENCE", "checkpoint rejected")
            conn.execute("UPDATE work_attempts SET last_heartbeat_at=? WHERE id=? AND lease_token=?", (time.time(), claim.attempt_id, claim.lease_token))
            self._sqlite_event(conn, aggregate_type="work_item", aggregate_id=claim.work_item_id, goal_run_id=claim.goal_run_id, event_type="work_item.checkpointed", payload=checkpoint_data, idempotency_key=f"checkpoint:{checkpoint_id}")
            return checkpoint_id

        if self.backend == "sqlite":
            return await self._guard_fenced(
                claim, "checkpoint", lambda: asyncio.to_thread(self._sqlite_tx, op)
            )

        async def op_pg(conn: Any) -> str:
            result = await conn.execute("UPDATE work_items SET checkpoint_id=$1,revision=revision+1,updated_at=$2 WHERE id=$3 AND lease_owner=$4 AND lease_token=$5 AND state IN ('leased','running')", checkpoint_id, time.time(), claim.work_item_id, claim.worker_id, claim.lease_token)
            if not result.endswith("1"):
                raise DurableExecutionError("STALE_FENCE", "checkpoint rejected")
            await conn.execute("UPDATE work_attempts SET last_heartbeat_at=$1 WHERE id=$2 AND lease_token=$3", time.time(), claim.attempt_id, claim.lease_token)
            await self._pg_event(conn, aggregate_type="work_item", aggregate_id=claim.work_item_id, goal_run_id=claim.goal_run_id, event_type="work_item.checkpointed", payload=checkpoint_data, idempotency_key=f"checkpoint:{checkpoint_id}")
            return checkpoint_id

        return await self._guard_fenced(claim, "checkpoint", lambda: self._pg_tx(op_pg))

    def _sqlite_fence(self, conn: Any, claim: ClaimEnvelope, *, states: tuple[str, ...] = ("leased", "running")) -> Any:
        placeholders = ",".join("?" for _ in states)
        row = conn.execute(
            f"SELECT * FROM work_items WHERE id=? AND lease_owner=? AND lease_token=? AND state IN ({placeholders}) AND lease_expires_at>?",
            (claim.work_item_id, claim.worker_id, claim.lease_token, *states, time.time()),
        ).fetchone()
        if row is None:
            raise DurableExecutionError("STALE_FENCE", "lease owner or fencing token is no longer current")
        return row

    async def declare_side_effect(
        self,
        *,
        goal_run_id: str,
        work_item_id: str,
        operation_key: str,
        operation_type: str,
        target_ref: str,
        request: Any,
        capability: str = "manual_only",
        probe_policy: str | None = None,
        claim: ClaimEnvelope | None = None,
    ) -> dict[str, Any]:
        """Record intent before an external call, returning the existing row on retry."""
        if not operation_key or request is None:
            raise DurableExecutionError("INVALID_SIDE_EFFECT", "operation key and request are required")
        if capability not in {"none", "idempotency_key", "status_probe", "compensation", "manual_only"}:
            raise DurableExecutionError("INVALID_SIDE_EFFECT", f"unknown provider capability: {capability}")
        request_hash = content_hash(request)
        now = time.time()

        def op(conn: Any) -> dict[str, Any]:
            if claim is not None:
                if claim.goal_run_id != goal_run_id or claim.work_item_id != work_item_id:
                    raise DurableExecutionError("STALE_FENCE", "side effect claim does not match work item")
                self._sqlite_fence(conn, claim)
            row = conn.execute("SELECT * FROM side_effects WHERE operation_key=?", (operation_key,)).fetchone()
            if row:
                if row["request_hash"] != request_hash:
                    raise DurableExecutionError("IDEMPOTENCY_CONFLICT", "operation key has a different request hash")
                requested_capability = probe_policy or capability
                if row["probe_policy"] and row["probe_policy"] != requested_capability:
                    raise DurableExecutionError("IDEMPOTENCY_CONFLICT", "operation key has a different capability policy")
                return dict(row)
            effect_id = new_id()
            conn.execute(
                "INSERT INTO side_effects(id,goal_run_id,work_item_id,operation_key,operation_type,target_ref,state,request_hash,probe_policy,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (effect_id, goal_run_id, work_item_id, operation_key, operation_type, target_ref, "declared", request_hash, probe_policy or capability, now, now),
            )
            self._sqlite_event(conn, aggregate_type="side_effect", aggregate_id=effect_id, goal_run_id=goal_run_id, event_type="side_effect.declared", payload={"operation_key": operation_key, "request_hash": request_hash, "capability": capability}, idempotency_key=f"side-effect-declared:{operation_key}")
            return dict(conn.execute("SELECT * FROM side_effects WHERE id=?", (effect_id,)).fetchone())

        if self.backend == "sqlite":
            if claim is None:
                return await asyncio.to_thread(self._sqlite_tx, op)
            return await self._guard_fenced(
                claim, "side_effect_declare", lambda: asyncio.to_thread(self._sqlite_tx, op)
            )

        async def op_pg(conn: Any) -> dict[str, Any]:
            if claim is not None:
                if claim.goal_run_id != goal_run_id or claim.work_item_id != work_item_id:
                    raise DurableExecutionError("STALE_FENCE", "side effect claim does not match work item")
                lease = await conn.fetchrow("SELECT 1 FROM execution_leases WHERE work_item_id=$1 AND owner_id=$2 AND token=$3 AND expires_at>$4 AND released_at IS NULL", claim.work_item_id, claim.worker_id, claim.lease_token, now)
                if lease is None:
                    raise DurableExecutionError("STALE_FENCE", "side effect claim is no longer current")
            row = await conn.fetchrow("SELECT * FROM side_effects WHERE operation_key=$1", operation_key)
            if row:
                if row["request_hash"] != request_hash:
                    raise DurableExecutionError("IDEMPOTENCY_CONFLICT", "operation key has a different request hash")
                requested_capability = probe_policy or capability
                if row["probe_policy"] and row["probe_policy"] != requested_capability:
                    raise DurableExecutionError("IDEMPOTENCY_CONFLICT", "operation key has a different capability policy")
                return dict(row)
            effect_id = new_id()
            await conn.execute("INSERT INTO side_effects(id,goal_run_id,work_item_id,operation_key,operation_type,target_ref,state,request_hash,probe_policy,first_seen_at,last_seen_at) VALUES($1,$2,$3,$4,$5,$6,'declared',$7,$8,$9,$9)", effect_id, goal_run_id, work_item_id, operation_key, operation_type, target_ref, request_hash, probe_policy or capability, now)
            await self._pg_event(conn, aggregate_type="side_effect", aggregate_id=effect_id, goal_run_id=goal_run_id, event_type="side_effect.declared", payload={"operation_key": operation_key, "request_hash": request_hash, "capability": capability}, idempotency_key=f"side-effect-declared:{operation_key}")
            return dict(await conn.fetchrow("SELECT * FROM side_effects WHERE id=$1", effect_id))

        if claim is None:
            return await self._pg_tx(op_pg)
        return await self._guard_fenced(
            claim, "side_effect_declare", lambda: self._pg_tx(op_pg)
        )

    async def update_side_effect(
        self,
        operation_key: str,
        *,
        state: str,
        provider_request_id: str | None = None,
        probe_result: dict[str, Any] | None = None,
        compensation: dict[str, Any] | None = None,
        claim: ClaimEnvelope | None = None,
    ) -> dict[str, Any]:
        allowed = {"declared", "started", "committed", "failed", "unknown", "compensated", "manual_review"}
        if state not in allowed:
            raise DurableExecutionError("INVALID_SIDE_EFFECT", state)
        now = time.time()

        def op(conn: Any) -> dict[str, Any]:
            row = conn.execute("SELECT * FROM side_effects WHERE operation_key=?", (operation_key,)).fetchone()
            if row is None:
                raise DurableExecutionError("NOT_FOUND", "side effect operation does not exist")
            if claim is not None:
                self._sqlite_fence(conn, claim)
                if row["goal_run_id"] != claim.goal_run_id or row["work_item_id"] != claim.work_item_id:
                    raise DurableExecutionError("STALE_FENCE", "side effect claim does not match operation")
            if row["state"] in {"committed", "compensated"} and state != row["state"]:
                raise DurableExecutionError("SIDE_EFFECT_TERMINAL", f"cannot transition {row['state']} to {state}")
            conn.execute(
                "UPDATE side_effects SET state=?,provider_request_id=COALESCE(?,provider_request_id),probe_result_json=COALESCE(?,probe_result_json),compensation_json=COALESCE(?,compensation_json),last_seen_at=?,revision=revision+1 WHERE operation_key=?",
                (state, provider_request_id, canonical_json(probe_result) if probe_result is not None else None, canonical_json(compensation) if compensation is not None else None, now, operation_key),
            )
            event_type = f"side_effect.{state}"
            self._sqlite_event(conn, aggregate_type="side_effect", aggregate_id=row["id"], goal_run_id=row["goal_run_id"], event_type=event_type, payload={"operation_key": operation_key, "provider_request_id": provider_request_id, "probe_result": probe_result}, idempotency_key=f"side-effect:{operation_key}:{state}:{int(now*1000)}")
            return dict(conn.execute("SELECT * FROM side_effects WHERE operation_key=?", (operation_key,)).fetchone())

        if self.backend == "sqlite":
            if claim is None:
                return await asyncio.to_thread(self._sqlite_tx, op)
            return await self._guard_fenced(
                claim, "side_effect_update", lambda: asyncio.to_thread(self._sqlite_tx, op)
            )

        async def op_pg(conn: Any) -> dict[str, Any]:
            row = await conn.fetchrow("SELECT * FROM side_effects WHERE operation_key=$1", operation_key)
            if row is None:
                raise DurableExecutionError("NOT_FOUND", "side effect operation does not exist")
            if claim is not None:
                if row["goal_run_id"] != claim.goal_run_id or row["work_item_id"] != claim.work_item_id:
                    raise DurableExecutionError("STALE_FENCE", "side effect claim does not match operation")
                lease = await conn.fetchrow("SELECT 1 FROM execution_leases WHERE work_item_id=$1 AND owner_id=$2 AND token=$3 AND expires_at>$4 AND released_at IS NULL", claim.work_item_id, claim.worker_id, claim.lease_token, now)
                if lease is None:
                    raise DurableExecutionError("STALE_FENCE", "side effect claim is no longer current")
            if row["state"] in {"committed", "compensated"} and state != row["state"]:
                raise DurableExecutionError("SIDE_EFFECT_TERMINAL", f"cannot transition {row['state']} to {state}")
            await conn.execute("UPDATE side_effects SET state=$1,provider_request_id=COALESCE($2,provider_request_id),probe_result_json=COALESCE($3,probe_result_json),compensation_json=COALESCE($4,compensation_json),last_seen_at=$5,revision=revision+1 WHERE operation_key=$6", state, provider_request_id, canonical_json(probe_result) if probe_result is not None else None, canonical_json(compensation) if compensation is not None else None, now, operation_key)
            await self._pg_event(conn, aggregate_type="side_effect", aggregate_id=row["id"], goal_run_id=row["goal_run_id"], event_type=f"side_effect.{state}", payload={"operation_key": operation_key, "provider_request_id": provider_request_id, "probe_result": probe_result}, idempotency_key=f"side-effect:{operation_key}:{state}:{int(now*1000)}")
            return dict(await conn.fetchrow("SELECT * FROM side_effects WHERE operation_key=$1", operation_key))

        if claim is None:
            return await self._pg_tx(op_pg)
        return await self._guard_fenced(
            claim, "side_effect_update", lambda: self._pg_tx(op_pg)
        )

    async def record_side_effect(self, operation_key: str, request_hash: str, state: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        """Compatibility API for callers that already computed a request hash."""
        if state not in {"declared", "started", "committed", "failed", "unknown", "manual_review"}:
            raise DurableExecutionError("INVALID_SIDE_EFFECT", state)

        def op(conn: Any) -> dict[str, Any]:
            row = conn.execute("SELECT * FROM side_effects WHERE operation_key=?", (operation_key,)).fetchone()
            if row is None:
                raise DurableExecutionError("NOT_FOUND", "side effect operation does not exist")
            if row["request_hash"] != request_hash:
                raise DurableExecutionError("IDEMPOTENCY_CONFLICT", "request hash mismatch")
            now = time.time()
            conn.execute("UPDATE side_effects SET state=?,probe_result_json=COALESCE(?,probe_result_json),last_seen_at=?,revision=revision+1 WHERE operation_key=?", (state, canonical_json(evidence) if evidence is not None else None, now, operation_key))
            return dict(conn.execute("SELECT * FROM side_effects WHERE operation_key=?", (operation_key,)).fetchone())

        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, op)

        async def op_pg(conn: Any) -> dict[str, Any]:
            row = await conn.fetchrow("SELECT * FROM side_effects WHERE operation_key=$1", operation_key)
            if row is None:
                raise DurableExecutionError("NOT_FOUND", "side effect operation does not exist")
            if row["request_hash"] != request_hash:
                raise DurableExecutionError("IDEMPOTENCY_CONFLICT", "request hash mismatch")
            now = time.time()
            await conn.execute("UPDATE side_effects SET state=$1,probe_result_json=COALESCE($2,probe_result_json),last_seen_at=$3,revision=revision+1 WHERE operation_key=$4", state, canonical_json(evidence) if evidence is not None else None, now, operation_key)
            return dict(await conn.fetchrow("SELECT * FROM side_effects WHERE operation_key=$1", operation_key))

        return await self._pg_tx(op_pg)

    async def get_side_effect(self, operation_key: str) -> dict[str, Any] | None:
        """Read one side-effect ledger row without exposing provider secrets."""
        def op(conn: Any) -> dict[str, Any] | None:
            row = conn.execute("SELECT * FROM side_effects WHERE operation_key=?", (operation_key,)).fetchone()
            return dict(row) if row is not None else None

        if self.backend == "sqlite":
            return await asyncio.to_thread(lambda: self._sqlite_read(op))

        async def op_pg(conn: Any) -> dict[str, Any] | None:
            row = await conn.fetchrow("SELECT * FROM side_effects WHERE operation_key=$1", operation_key)
            return dict(row) if row is not None else None

        return await self._pg_tx(op_pg)

    def _sqlite_release_lease(self, conn: Any, claim: ClaimEnvelope, *, reason: str, now: float) -> None:
        conn.execute("UPDATE execution_leases SET released_at=?,release_reason=? WHERE work_item_id=? AND owner_id=? AND token=? AND released_at IS NULL", (now, reason, claim.work_item_id, claim.worker_id, claim.lease_token))
        conn.execute("UPDATE work_items SET lease_owner=NULL,lease_expires_at=NULL,last_heartbeat_at=NULL WHERE id=? AND lease_owner=? AND lease_token=?", (claim.work_item_id, claim.worker_id, claim.lease_token))

    @staticmethod
    def _dependencies_advance_sqlite(conn: Any, goal_run_id: str, completed_id: str) -> list[str]:
        promoted: list[str] = []
        rows = conn.execute("SELECT * FROM work_items WHERE goal_run_id=? AND state='created'", (goal_run_id,)).fetchall()
        for row in rows:
            dependencies = _decode(row["dependency_json"], [])
            if not dependencies:
                continue
            states = conn.execute("SELECT logical_key,state FROM work_items WHERE goal_run_id=? AND logical_key IN (" + ",".join("?" for _ in dependencies) + ")", (goal_run_id, *dependencies)).fetchall()
            by_key = {item["logical_key"]: item["state"] for item in states}
            if all(by_key.get(dep) == "succeeded" for dep in dependencies):
                conn.execute("UPDATE work_items SET state='ready',revision=revision+1,updated_at=? WHERE id=? AND state='created'", (time.time(), row["id"]))
                promoted.append(row["id"])
        return promoted

    async def complete(
        self,
        claim: ClaimEnvelope,
        result: dict[str, Any],
        *,
        usage: dict[str, Any] | None = None,
        artifact_manifest_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise DurableExecutionError("INVALID_RESULT", "completion result must be a mapping")
        result_hash = content_hash(result)
        result_json = canonical_json(result)
        usage_json = canonical_json(usage or {})
        now = time.time()

        def op(conn: Any) -> dict[str, Any]:
            current = conn.execute("SELECT * FROM work_items WHERE id=?", (claim.work_item_id,)).fetchone()
            if current is None:
                raise DurableExecutionError("NOT_FOUND", "work item does not exist")
            if current["state"] == "succeeded":
                if current["result_hash"] == result_hash:
                    self._sqlite_event(
                        conn,
                        aggregate_type="work_item",
                        aggregate_id=claim.work_item_id,
                        goal_run_id=claim.goal_run_id,
                        event_type="work_item.completion_deduplicated",
                        payload={"attempt_id": claim.attempt_id, "result_hash": result_hash},
                        idempotency_key=f"completion-deduplicated:{claim.work_item_id}:{result_hash}",
                    )
                    return {"status": "idempotent", "result_hash": result_hash, "work_item_id": claim.work_item_id}
                raise DurableExecutionError("COMPLETION_CONFLICT", "terminal item has a different result hash")
            self._sqlite_fence(conn, claim)
            unresolved = conn.execute("SELECT operation_key,state FROM side_effects WHERE work_item_id=? AND state IN ('started','unknown')", (claim.work_item_id,)).fetchall()
            if unresolved:
                raise DurableExecutionError("SAFETY_HOLD", "side effect outcome is unresolved")
            changed = conn.execute("UPDATE work_items SET state='succeeded',result_json=?,result_hash=?,checkpoint_id=COALESCE(?,checkpoint_id),revision=revision+1,updated_at=? WHERE id=? AND lease_owner=? AND lease_token=?", (result_json, result_hash, artifact_manifest_id, now, claim.work_item_id, claim.worker_id, claim.lease_token)).rowcount
            if changed != 1:
                raise DurableExecutionError("STALE_FENCE", "completion lost ownership")
            conn.execute("UPDATE work_attempts SET state='succeeded',ended_at=?,result_hash=?,usage_json=? WHERE id=? AND lease_token=?", (now, result_hash, usage_json, claim.attempt_id, claim.lease_token))
            self._sqlite_release_lease(conn, claim, reason="completed", now=now)
            self._sqlite_event(conn, aggregate_type="work_item", aggregate_id=claim.work_item_id, goal_run_id=claim.goal_run_id, event_type="work_item.succeeded", payload={"attempt_id": claim.attempt_id, "result_hash": result_hash, "artifact_manifest_id": artifact_manifest_id}, idempotency_key=f"succeeded:{claim.attempt_id}:{result_hash}")
            promoted_items = self._dependencies_advance_sqlite(conn, claim.goal_run_id, claim.work_item_id)
            for promoted in promoted_items:
                self._sqlite_event(conn, aggregate_type="work_item", aggregate_id=promoted, goal_run_id=claim.goal_run_id, event_type="work_item.ready", payload={"reason": "dependencies_succeeded", "completed_work_item_id": claim.work_item_id}, idempotency_key=f"ready:{promoted}:{claim.work_item_id}")
            return {"status": "committed", "result_hash": result_hash, "work_item_id": claim.work_item_id, "promoted": len(promoted_items)}

        if self.backend == "sqlite":
            return await self._guard_fenced(
                claim, "complete", lambda: asyncio.to_thread(self._sqlite_tx, op)
            )

        async def op_pg(conn: Any) -> dict[str, Any]:
            current = await conn.fetchrow("SELECT * FROM work_items WHERE id=$1 FOR UPDATE", claim.work_item_id)
            if current is None:
                raise DurableExecutionError("NOT_FOUND", "work item does not exist")
            if current["state"] == "succeeded":
                if current["result_hash"] == result_hash:
                    await self._pg_event(
                        conn,
                        aggregate_type="work_item",
                        aggregate_id=claim.work_item_id,
                        goal_run_id=claim.goal_run_id,
                        event_type="work_item.completion_deduplicated",
                        payload={"attempt_id": claim.attempt_id, "result_hash": result_hash},
                        idempotency_key=f"completion-deduplicated:{claim.work_item_id}:{result_hash}",
                    )
                    return {"status": "idempotent", "result_hash": result_hash, "work_item_id": claim.work_item_id}
                raise DurableExecutionError("COMPLETION_CONFLICT", "terminal item has a different result hash")
            lease = await conn.fetchrow("SELECT 1 FROM execution_leases WHERE work_item_id=$1 AND owner_id=$2 AND token=$3 AND expires_at>$4 AND released_at IS NULL", claim.work_item_id, claim.worker_id, claim.lease_token, now)
            if lease is None:
                raise DurableExecutionError("STALE_FENCE", "lease owner or fencing token is no longer current")
            unresolved = await conn.fetch("SELECT operation_key,state FROM side_effects WHERE work_item_id=$1 AND state IN ('started','unknown')", claim.work_item_id)
            if unresolved:
                raise DurableExecutionError("SAFETY_HOLD", "side effect outcome is unresolved")
            updated = await conn.execute("UPDATE work_items SET state='succeeded',result_json=$1,result_hash=$2,checkpoint_id=COALESCE($3,checkpoint_id),revision=revision+1,updated_at=$4 WHERE id=$5 AND lease_owner=$6 AND lease_token=$7", result_json, result_hash, artifact_manifest_id, now, claim.work_item_id, claim.worker_id, claim.lease_token)
            if not updated.endswith("1"):
                raise DurableExecutionError("STALE_FENCE", "completion lost ownership")
            await conn.execute("UPDATE work_attempts SET state='succeeded',ended_at=$1,result_hash=$2,usage_json=$3 WHERE id=$4 AND lease_token=$5", now, result_hash, usage_json, claim.attempt_id, claim.lease_token)
            await conn.execute("UPDATE execution_leases SET released_at=$1,release_reason='completed' WHERE work_item_id=$2 AND owner_id=$3 AND token=$4 AND released_at IS NULL", now, claim.work_item_id, claim.worker_id, claim.lease_token)
            await conn.execute("UPDATE work_items SET lease_owner=NULL,lease_expires_at=NULL,last_heartbeat_at=NULL WHERE id=$1 AND lease_owner=$2 AND lease_token=$3", claim.work_item_id, claim.worker_id, claim.lease_token)
            await self._pg_event(conn, aggregate_type="work_item", aggregate_id=claim.work_item_id, goal_run_id=claim.goal_run_id, event_type="work_item.succeeded", payload={"attempt_id": claim.attempt_id, "result_hash": result_hash, "artifact_manifest_id": artifact_manifest_id}, idempotency_key=f"succeeded:{claim.attempt_id}:{result_hash}")
            promoted = 0
            rows = await conn.fetch("SELECT * FROM work_items WHERE goal_run_id=$1 AND state='created'", claim.goal_run_id)
            for row in rows:
                deps = _decode(row["dependency_json"], [])
                if not deps:
                    continue
                dep_rows = await conn.fetch("SELECT logical_key,state FROM work_items WHERE goal_run_id=$1 AND logical_key=ANY($2::text[])", claim.goal_run_id, list(deps))
                if all({item["logical_key"]: item["state"] for item in dep_rows}.get(dep) == "succeeded" for dep in deps):
                    await conn.execute("UPDATE work_items SET state='ready',revision=revision+1,updated_at=$1 WHERE id=$2 AND state='created'", now, row["id"])
                    await self._pg_event(conn, aggregate_type="work_item", aggregate_id=row["id"], goal_run_id=claim.goal_run_id, event_type="work_item.ready", payload={"reason": "dependencies_succeeded", "completed_work_item_id": claim.work_item_id}, idempotency_key=f"ready:{row['id']}:{claim.work_item_id}")
                    promoted += 1
            return {"status": "committed", "result_hash": result_hash, "work_item_id": claim.work_item_id, "promoted": promoted}

        return await self._guard_fenced(claim, "complete", lambda: self._pg_tx(op_pg))

    async def fail(
        self,
        claim: ClaimEnvelope,
        failure: dict[str, Any] | str,
        *,
        classification: str = "permanent_failure",
        retry_delay_s: float = 1.0,
    ) -> dict[str, Any]:
        """Record immutable attempt failure before deciding requeue/quarantine."""
        error = failure if isinstance(failure, dict) else {"message": str(failure)}
        if classification not in {"safe_retry", "idempotent_retry", "unknown", "permanent_failure", "cancelled"}:
            raise DurableExecutionError("INVALID_FAILURE_CLASS", classification)
        now = time.time()

        def op(conn: Any) -> dict[str, Any]:
            current = conn.execute("SELECT * FROM work_items WHERE id=?", (claim.work_item_id,)).fetchone()
            if current is None:
                raise DurableExecutionError("NOT_FOUND", "work item does not exist")
            if current["state"] in TERMINAL_WORK_STATES:
                return {"status": "idempotent", "state": current["state"], "work_item_id": claim.work_item_id}
            self._sqlite_fence(conn, claim)
            if classification in {"safe_retry", "idempotent_retry"} and int(current["attempt_count"]) < int(current["max_attempts"]):
                state = "retry_wait"
                recovery_state = "classified"
                next_ready = now + max(0.0, retry_delay_s)
                decision = "RETRY_SAFE" if classification == "safe_retry" else "IDEMPOTENT_RETRY"
                attempt_state = "failed"
            elif classification == "unknown":
                state = "unknown"
                recovery_state = "pending"
                next_ready = now
                decision = "QUARANTINED_UNKNOWN"
                attempt_state = "unknown"
            elif classification == "cancelled":
                state = "cancelled"
                recovery_state = "classified"
                next_ready = now
                decision = "CANCELLED"
                attempt_state = "cancelled"
            else:
                state = "failed"
                recovery_state = "classified"
                next_ready = now
                decision = "PERMANENT_FAILURE"
                attempt_state = "failed"
            conn.execute("UPDATE work_items SET state=?,next_ready_at=?,error_json=?,recovery_state=?,revision=revision+1,updated_at=? WHERE id=? AND lease_owner=? AND lease_token=?", (state, next_ready, canonical_json(error), recovery_state, now, claim.work_item_id, claim.worker_id, claim.lease_token))
            conn.execute("UPDATE work_attempts SET state=?,ended_at=?,error_json=?,unknown_reason=? WHERE id=? AND lease_token=?", (attempt_state, now, canonical_json(error), error.get("unknown_reason") if isinstance(error, dict) else None, claim.attempt_id, claim.lease_token))
            self._sqlite_release_lease(conn, claim, reason=decision, now=now)
            self._sqlite_event(conn, aggregate_type="work_item", aggregate_id=claim.work_item_id, goal_run_id=claim.goal_run_id, event_type="work_item.unknown" if state == "unknown" else "work_item.failed" if state == "failed" else "work_item.cancelled", payload={"attempt_id": claim.attempt_id, "classification": classification, "error": error, "next_ready_at": next_ready}, idempotency_key=f"terminal:{claim.attempt_id}:{decision}")
            return {"status": decision, "state": state, "work_item_id": claim.work_item_id, "next_ready_at": next_ready}

        if self.backend == "sqlite":
            return await self._guard_fenced(
                claim, "fail", lambda: asyncio.to_thread(self._sqlite_tx, op)
            )

        async def op_pg(conn: Any) -> dict[str, Any]:
            current = await conn.fetchrow("SELECT * FROM work_items WHERE id=$1 FOR UPDATE", claim.work_item_id)
            if current is None:
                raise DurableExecutionError("NOT_FOUND", "work item does not exist")
            if current["state"] in TERMINAL_WORK_STATES:
                return {"status": "idempotent", "state": current["state"], "work_item_id": claim.work_item_id}
            lease = await conn.fetchrow("SELECT 1 FROM execution_leases WHERE work_item_id=$1 AND owner_id=$2 AND token=$3 AND expires_at>$4 AND released_at IS NULL", claim.work_item_id, claim.worker_id, claim.lease_token, now)
            if lease is None:
                raise DurableExecutionError("STALE_FENCE", "lease owner or fencing token is no longer current")
            if classification in {"safe_retry", "idempotent_retry"} and int(current["attempt_count"]) < int(current["max_attempts"]):
                state, recovery_state, decision, attempt_state = "retry_wait", "classified", "RETRY_SAFE" if classification == "safe_retry" else "IDEMPOTENT_RETRY", "failed"
                next_ready = now + max(0.0, retry_delay_s)
            elif classification == "unknown":
                state, recovery_state, decision, attempt_state, next_ready = "unknown", "pending", "QUARANTINED_UNKNOWN", "unknown", now
            elif classification == "cancelled":
                state, recovery_state, decision, attempt_state, next_ready = "cancelled", "classified", "CANCELLED", "cancelled", now
            else:
                state, recovery_state, decision, attempt_state, next_ready = "failed", "classified", "PERMANENT_FAILURE", "failed", now
            await conn.execute("UPDATE work_items SET state=$1,next_ready_at=$2,error_json=$3,recovery_state=$4,revision=revision+1,updated_at=$5 WHERE id=$6 AND lease_owner=$7 AND lease_token=$8", state, next_ready, canonical_json(error), recovery_state, now, claim.work_item_id, claim.worker_id, claim.lease_token)
            await conn.execute("UPDATE work_attempts SET state=$1,ended_at=$2,error_json=$3,unknown_reason=$4 WHERE id=$5 AND lease_token=$6", attempt_state, now, canonical_json(error), error.get("unknown_reason") if isinstance(error, dict) else None, claim.attempt_id, claim.lease_token)
            await conn.execute("UPDATE execution_leases SET released_at=$1,release_reason=$2 WHERE work_item_id=$3 AND owner_id=$4 AND token=$5 AND released_at IS NULL", now, decision, claim.work_item_id, claim.worker_id, claim.lease_token)
            await conn.execute("UPDATE work_items SET lease_owner=NULL,lease_expires_at=NULL,last_heartbeat_at=NULL WHERE id=$1 AND lease_owner=$2 AND lease_token=$3", claim.work_item_id, claim.worker_id, claim.lease_token)
            event_type = "work_item.unknown" if state == "unknown" else "work_item.failed" if state == "failed" else "work_item.cancelled"
            await self._pg_event(conn, aggregate_type="work_item", aggregate_id=claim.work_item_id, goal_run_id=claim.goal_run_id, event_type=event_type, payload={"attempt_id": claim.attempt_id, "classification": classification, "error": error, "next_ready_at": next_ready}, idempotency_key=f"terminal:{claim.attempt_id}:{decision}")
            return {"status": decision, "state": state, "work_item_id": claim.work_item_id, "next_ready_at": next_ready}

        return await self._guard_fenced(claim, "fail", lambda: self._pg_tx(op_pg))

    async def request_cancel(self, goal_run_id: str, *, actor: str = "user") -> dict[str, Any]:
        now = time.time()

        def op(conn: Any) -> dict[str, Any]:
            goal = conn.execute("SELECT * FROM goal_runs WHERE id=?", (goal_run_id,)).fetchone()
            if goal is None:
                raise DurableExecutionError("NOT_FOUND", "goal run does not exist")
            if goal["status"] in TERMINAL_GOAL_STATES:
                return dict(goal)
            if goal["status"] == "cancelling":
                return dict(goal)
            conn.execute("UPDATE goal_runs SET status='cancelling',cancellation_requested_at=?,revision=revision+1,updated_at=? WHERE id=?", (now, now, goal_run_id))
            rows = conn.execute("SELECT id FROM work_items WHERE goal_run_id=? AND kind<>'finalize' AND state IN ('created','ready','retry_wait')", (goal_run_id,)).fetchall()
            for row in rows:
                conn.execute("UPDATE work_items SET state='cancelled',recovery_state='classified',revision=revision+1,updated_at=? WHERE id=?", (now, row["id"]))
                self._sqlite_event(conn, aggregate_type="work_item", aggregate_id=row["id"], goal_run_id=goal_run_id, event_type="work_item.cancelled", payload={"source": actor, "requested_at": now}, idempotency_key=f"cancel:{goal_run_id}:{row['id']}")
            self._sqlite_event(conn, aggregate_type="goal_run", aggregate_id=goal_run_id, goal_run_id=goal_run_id, event_type="goal_run.status_changed", payload={"from": goal["status"], "to": "cancelling", "actor": actor}, idempotency_key=f"goal-cancel:{goal_run_id}:{int(now*1000)}")
            return dict(conn.execute("SELECT * FROM goal_runs WHERE id=?", (goal_run_id,)).fetchone())

        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, op)

        async def op_pg(conn: Any) -> dict[str, Any]:
            goal = await conn.fetchrow("SELECT * FROM goal_runs WHERE id=$1 FOR UPDATE", goal_run_id)
            if goal is None:
                raise DurableExecutionError("NOT_FOUND", "goal run does not exist")
            if goal["status"] in TERMINAL_GOAL_STATES:
                return dict(goal)
            if goal["status"] == "cancelling":
                return dict(goal)
            await conn.execute("UPDATE goal_runs SET status='cancelling',cancellation_requested_at=$1,revision=revision+1,updated_at=$2 WHERE id=$3", now, now, goal_run_id)
            rows = await conn.fetch("SELECT id FROM work_items WHERE goal_run_id=$1 AND kind<>'finalize' AND state IN ('created','ready','retry_wait') FOR UPDATE", goal_run_id)
            for row in rows:
                await conn.execute("UPDATE work_items SET state='cancelled',recovery_state='classified',revision=revision+1,updated_at=$1 WHERE id=$2", now, row["id"])
                await self._pg_event(conn, aggregate_type="work_item", aggregate_id=row["id"], goal_run_id=goal_run_id, event_type="work_item.cancelled", payload={"source": actor, "requested_at": now}, idempotency_key=f"cancel:{goal_run_id}:{row['id']}")
            await self._pg_event(conn, aggregate_type="goal_run", aggregate_id=goal_run_id, goal_run_id=goal_run_id, event_type="goal_run.status_changed", payload={"from": goal["status"], "to": "cancelling", "actor": actor}, idempotency_key=f"goal-cancel:{goal_run_id}:{int(now*1000)}")
            return dict(await conn.fetchrow("SELECT * FROM goal_runs WHERE id=$1", goal_run_id))

        return await self._pg_tx(op_pg)

    async def register_worker(self, worker_id: str, *, incarnation_id: str | None = None, process_id: str | None = None) -> dict[str, Any]:
        incarnation = incarnation_id or new_id()
        process = process_id or f"{socket.gethostname()}:{os.getpid()}"
        now = time.time()

        def op(conn: Any) -> dict[str, Any]:
            conn.execute("INSERT OR REPLACE INTO worker_registry(worker_id,process_id,state,incarnation_id,started_at,last_seen_at,draining_at) VALUES(?,?,?,?,?,?,NULL)", (worker_id, process, "ready", incarnation, now, now))
            return dict(conn.execute("SELECT * FROM worker_registry WHERE worker_id=?", (worker_id,)).fetchone())

        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, op)

        async def op_pg(conn: Any) -> dict[str, Any]:
            await conn.execute("INSERT INTO worker_registry(worker_id,process_id,state,incarnation_id,started_at,last_seen_at) VALUES($1,$2,'ready',$3,$4,$4) ON CONFLICT(worker_id) DO UPDATE SET process_id=$2,state='ready',incarnation_id=$3,started_at=$4,last_seen_at=$4,draining_at=NULL", worker_id, process, incarnation, now)
            return dict(await conn.fetchrow("SELECT * FROM worker_registry WHERE worker_id=$1", worker_id))

        return await self._pg_tx(op_pg)

    async def drain_worker(self, worker_id: str) -> None:
        now = time.time()

        def op(conn: Any) -> None:
            conn.execute("UPDATE worker_registry SET state='draining',draining_at=?,last_seen_at=? WHERE worker_id=?", (now, now, worker_id))

        if self.backend == "sqlite":
            await asyncio.to_thread(self._sqlite_tx, op)
            return

        async def op_pg(conn: Any) -> None:
            await conn.execute("UPDATE worker_registry SET state='draining',draining_at=$1,last_seen_at=$1 WHERE worker_id=$2", now, worker_id)

        await self._pg_tx(op_pg)

    async def ensure_finalization_item(self, goal_run_id: str, *, snapshot_hash: str | None = None) -> dict[str, Any]:
        """Create the single durable finalizer item, idempotently."""
        payload = {"snapshot_hash": snapshot_hash} if snapshot_hash else {}
        item = WorkItemSpec(
            goal_run_id=goal_run_id,
            logical_key="__finalization__",
            kind="finalize",
            payload=payload,
            parallel=False,
            idempotency_key=f"{goal_run_id}:finalization",
            max_attempts=100,
        )
        row = await self.enqueue_work_item(item)
        now = time.time()
        work_id = row["id"]

        def op(conn: Any) -> dict[str, Any]:
            previous = conn.execute("SELECT status,finalization_state FROM goal_runs WHERE id=?", (goal_run_id,)).fetchone()
            next_status = "finalizing" if previous and previous["status"] == "running" else (previous["status"] if previous else "finalizing")
            next_finalization_state = "running" if previous and previous["finalization_state"] == "not_started" else (previous["finalization_state"] if previous else "running")
            conn.execute("UPDATE goal_runs SET status=?,finalization_state=?,finalization_item_id=?,revision=revision+1,updated_at=? WHERE id=?", (next_status, next_finalization_state, work_id, now, goal_run_id))
            if previous and previous["status"] != next_status:
                self._sqlite_event(
                    conn,
                    aggregate_type="goal_run",
                    aggregate_id=goal_run_id,
                    goal_run_id=goal_run_id,
                    event_type="goal_run.status_changed",
                    payload={"from": previous["status"], "to": next_status, "reason": "finalization_started"},
                    idempotency_key=f"goal-status-finalizing:{goal_run_id}",
                )
            self._sqlite_event(
                conn,
                aggregate_type="goal_run",
                aggregate_id=goal_run_id,
                goal_run_id=goal_run_id,
                event_type="finalization.started",
                payload={"work_item_id": work_id, "snapshot_hash": snapshot_hash},
                idempotency_key=f"finalization-started:{goal_run_id}:{work_id}",
            )
            return dict(conn.execute("SELECT * FROM work_items WHERE id=?", (work_id,)).fetchone())

        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, op)

        async def op_pg(conn: Any) -> dict[str, Any]:
            previous = await conn.fetchrow("SELECT status,finalization_state FROM goal_runs WHERE id=$1 FOR UPDATE", goal_run_id)
            next_status = "finalizing" if previous and previous["status"] == "running" else (previous["status"] if previous else "finalizing")
            next_finalization_state = "running" if previous and previous["finalization_state"] == "not_started" else (previous["finalization_state"] if previous else "running")
            await conn.execute("UPDATE goal_runs SET status=$1,finalization_state=$2,finalization_item_id=$3,revision=revision+1,updated_at=$4 WHERE id=$5", next_status, next_finalization_state, work_id, now, goal_run_id)
            if previous and previous["status"] != next_status:
                await self._pg_event(
                    conn,
                    aggregate_type="goal_run",
                    aggregate_id=goal_run_id,
                    goal_run_id=goal_run_id,
                    event_type="goal_run.status_changed",
                    payload={"from": previous["status"], "to": next_status, "reason": "finalization_started"},
                    idempotency_key=f"goal-status-finalizing:{goal_run_id}",
                )
            await self._pg_event(conn, aggregate_type="goal_run", aggregate_id=goal_run_id, goal_run_id=goal_run_id, event_type="finalization.started", payload={"work_item_id": work_id, "snapshot_hash": snapshot_hash}, idempotency_key=f"finalization-started:{goal_run_id}:{work_id}")
            return dict(await conn.fetchrow("SELECT * FROM work_items WHERE id=$1", work_id))

        return await self._pg_tx(op_pg)

    async def resume_finalization(self, goal_run_id: str, *, worker_id: str, lease_ttl_s: float = 30.0) -> ClaimEnvelope | None:
        """Claim the single finalizer item without re-running child work."""
        goal = await self.get_goal_run(goal_run_id)
        if goal is not None and goal["status"] in TERMINAL_GOAL_STATES:
            return None
        item = await self.ensure_finalization_item(goal_run_id)
        if item["state"] == "succeeded":
            return None
        was_attempted = int(item.get("attempt_count") or 0) > 0
        claim = await self.claim_next(
            worker_id,
            capabilities={"*"},
            kinds={"finalize"},
            goal_run_id=goal_run_id,
            lease_ttl_s=lease_ttl_s,
        )
        if claim is None or not was_attempted:
            return claim

        def op(conn: Any) -> None:
            self._sqlite_event(
                conn,
                aggregate_type="goal_run",
                aggregate_id=goal_run_id,
                goal_run_id=goal_run_id,
                event_type="finalization.resumed",
                payload={"work_item_id": claim.work_item_id, "attempt_id": claim.attempt_id, "lease_token": claim.lease_token},
                idempotency_key=f"finalization-resumed:{goal_run_id}:{claim.attempt_id}",
            )

        if self.backend == "sqlite":
            await asyncio.to_thread(self._sqlite_tx, op)
        else:
            async def op_pg(conn: Any) -> None:
                await self._pg_event(
                    conn,
                    aggregate_type="goal_run",
                    aggregate_id=goal_run_id,
                    goal_run_id=goal_run_id,
                    event_type="finalization.resumed",
                    payload={"work_item_id": claim.work_item_id, "attempt_id": claim.attempt_id, "lease_token": claim.lease_token},
                    idempotency_key=f"finalization-resumed:{goal_run_id}:{claim.attempt_id}",
                )

            await self._pg_tx(op_pg)
        return claim

    async def complete_finalization(
        self,
        claim: ClaimEnvelope,
        result: dict[str, Any],
        *,
        final_status: str,
        snapshot_hash: str,
        result_artifact_id: str | None = None,
        resumed: bool = False,
    ) -> dict[str, Any]:
        """Commit finalizer output and GoalRun terminal transition atomically."""
        if final_status not in {"completed", "partial_completed", "failed", "cancelled", "blocked"}:
            raise DurableExecutionError("INVALID_FINAL_STATUS", final_status)
        result_hash = content_hash(result)
        result_json = canonical_json(result)
        now = time.time()

        def op(conn: Any) -> dict[str, Any]:
            nonlocal final_status, result, result_hash, result_json
            item = conn.execute("SELECT * FROM work_items WHERE id=?", (claim.work_item_id,)).fetchone()
            if item is None:
                raise DurableExecutionError("NOT_FOUND", "finalization item does not exist")
            if item["state"] == "succeeded":
                if item["result_hash"] == result_hash:
                    expected_state = "partial_completed" if final_status == "partial_completed" else final_status
                    goal = conn.execute("SELECT status,finalization_state,result_artifact_id FROM goal_runs WHERE id=?", (claim.goal_run_id,)).fetchone()
                    if goal is not None and (goal["status"] != final_status or goal["finalization_state"] != expected_state or goal["result_artifact_id"] != result_artifact_id):
                        conn.execute("UPDATE goal_runs SET status=?,finalization_state=?,result_artifact_id=?,revision=revision+1,updated_at=? WHERE id=?", (final_status, expected_state, result_artifact_id, now, claim.goal_run_id))
                        self._sqlite_event(
                            conn,
                            aggregate_type="goal_run",
                            aggregate_id=claim.goal_run_id,
                            goal_run_id=claim.goal_run_id,
                            event_type="goal_run.status_changed",
                            payload={"from": goal["status"], "to": final_status, "reason": "finalization_commit_repaired"},
                            idempotency_key=f"goal-status-finalization-repair:{claim.goal_run_id}:{result_hash}",
                        )
                    return {"status": "idempotent", "final_status": final_status, "result_hash": result_hash}
                raise DurableExecutionError("COMPLETION_CONFLICT", "finalization has a different result hash")
            self._sqlite_fence(conn, claim)
            pending_children = conn.execute(
                "SELECT id,logical_key FROM work_items WHERE goal_run_id=? AND id<>? AND state IN ('created','ready','retry_wait')",
                (claim.goal_run_id, claim.work_item_id),
            ).fetchall()
            if pending_children and final_status == "completed":
                final_status = "partial_completed"
                result = {**result, "incomplete_work": [child["logical_key"] for child in pending_children]}
                result_hash = content_hash(result)
                result_json = canonical_json(result)
            for child in pending_children:
                conn.execute(
                    "UPDATE work_items SET state='cancelled',recovery_state='classified',error_json=?,revision=revision+1,updated_at=? WHERE id=? AND state IN ('created','ready','retry_wait')",
                    (canonical_json({"message": "finalization closed before child execution"}), now, child["id"]),
                )
                self._sqlite_event(
                    conn,
                    aggregate_type="work_item",
                    aggregate_id=child["id"],
                    goal_run_id=claim.goal_run_id,
                    event_type="work_item.cancelled",
                    payload={"source": "finalization", "reason": "finalization_closed"},
                    idempotency_key=f"finalization-cancelled:{claim.goal_run_id}:{child['id']}",
                )
            conn.execute("UPDATE work_items SET state='succeeded',result_json=?,result_hash=?,revision=revision+1,updated_at=? WHERE id=? AND lease_owner=? AND lease_token=?", (result_json, result_hash, now, claim.work_item_id, claim.worker_id, claim.lease_token))
            conn.execute("UPDATE work_attempts SET state='succeeded',ended_at=?,result_hash=?,usage_json=? WHERE id=? AND lease_token=?", (now, result_hash, canonical_json({"snapshot_hash": snapshot_hash, "resumed": resumed}), claim.attempt_id, claim.lease_token))
            self._sqlite_release_lease(conn, claim, reason="finalization_completed", now=now)
            conn.execute("UPDATE goal_runs SET status=?,finalization_state=?,result_artifact_id=?,revision=revision+1,updated_at=? WHERE id=?", (final_status, "partial_completed" if final_status == "partial_completed" else final_status, result_artifact_id, now, claim.goal_run_id))
            event_type = "finalization.partial_completed" if final_status == "partial_completed" else "finalization.completed"
            self._sqlite_event(conn, aggregate_type="goal_run", aggregate_id=claim.goal_run_id, goal_run_id=claim.goal_run_id, event_type=event_type, payload={"snapshot_hash": snapshot_hash, "result_hash": result_hash, "result_artifact_id": result_artifact_id, "resumed": resumed, "status": final_status}, idempotency_key=f"finalization-result:{claim.goal_run_id}:{snapshot_hash}:{result_hash}")
            return {"status": "committed", "final_status": final_status, "result_hash": result_hash, "result_artifact_id": result_artifact_id}

        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, op)

        async def op_pg(conn: Any) -> dict[str, Any]:
            nonlocal final_status, result, result_hash, result_json
            item = await conn.fetchrow("SELECT * FROM work_items WHERE id=$1 FOR UPDATE", claim.work_item_id)
            if item is None:
                raise DurableExecutionError("NOT_FOUND", "finalization item does not exist")
            if item["state"] == "succeeded":
                if item["result_hash"] == result_hash:
                    expected_state = "partial_completed" if final_status == "partial_completed" else final_status
                    goal = await conn.fetchrow("SELECT status,finalization_state,result_artifact_id FROM goal_runs WHERE id=$1 FOR UPDATE", claim.goal_run_id)
                    if goal is not None and (goal["status"] != final_status or goal["finalization_state"] != expected_state or goal["result_artifact_id"] != result_artifact_id):
                        await conn.execute("UPDATE goal_runs SET status=$1,finalization_state=$2,result_artifact_id=$3,revision=revision+1,updated_at=$4 WHERE id=$5", final_status, expected_state, result_artifact_id, now, claim.goal_run_id)
                        await self._pg_event(
                            conn,
                            aggregate_type="goal_run",
                            aggregate_id=claim.goal_run_id,
                            goal_run_id=claim.goal_run_id,
                            event_type="goal_run.status_changed",
                            payload={"from": goal["status"], "to": final_status, "reason": "finalization_commit_repaired"},
                            idempotency_key=f"goal-status-finalization-repair:{claim.goal_run_id}:{result_hash}",
                        )
                    return {"status": "idempotent", "final_status": final_status, "result_hash": result_hash}
                raise DurableExecutionError("COMPLETION_CONFLICT", "finalization has a different result hash")
            lease = await conn.fetchrow("SELECT 1 FROM execution_leases WHERE work_item_id=$1 AND owner_id=$2 AND token=$3 AND expires_at>$4 AND released_at IS NULL", claim.work_item_id, claim.worker_id, claim.lease_token, now)
            if lease is None:
                raise DurableExecutionError("STALE_FENCE", "finalization lease is no longer current")
            pending_children = await conn.fetch(
                "SELECT id,logical_key FROM work_items WHERE goal_run_id=$1 AND id<>$2 AND state IN ('created','ready','retry_wait') FOR UPDATE",
                claim.goal_run_id,
                claim.work_item_id,
            )
            if pending_children and final_status == "completed":
                final_status = "partial_completed"
                result = {**result, "incomplete_work": [child["logical_key"] for child in pending_children]}
                result_hash = content_hash(result)
                result_json = canonical_json(result)
            for child in pending_children:
                await conn.execute(
                    "UPDATE work_items SET state='cancelled',recovery_state='classified',error_json=$1,revision=revision+1,updated_at=$2 WHERE id=$3 AND state IN ('created','ready','retry_wait')",
                    canonical_json({"message": "finalization closed before child execution"}),
                    now,
                    child["id"],
                )
                await self._pg_event(
                    conn,
                    aggregate_type="work_item",
                    aggregate_id=child["id"],
                    goal_run_id=claim.goal_run_id,
                    event_type="work_item.cancelled",
                    payload={"source": "finalization", "reason": "finalization_closed"},
                    idempotency_key=f"finalization-cancelled:{claim.goal_run_id}:{child['id']}",
                )
            await conn.execute("UPDATE work_items SET state='succeeded',result_json=$1,result_hash=$2,revision=revision+1,updated_at=$3 WHERE id=$4 AND lease_owner=$5 AND lease_token=$6", result_json, result_hash, now, claim.work_item_id, claim.worker_id, claim.lease_token)
            await conn.execute("UPDATE work_attempts SET state='succeeded',ended_at=$1,result_hash=$2,usage_json=$3 WHERE id=$4 AND lease_token=$5", now, result_hash, canonical_json({"snapshot_hash": snapshot_hash, "resumed": resumed}), claim.attempt_id, claim.lease_token)
            await conn.execute("UPDATE execution_leases SET released_at=$1,release_reason='finalization_completed' WHERE work_item_id=$2 AND owner_id=$3 AND token=$4 AND released_at IS NULL", now, claim.work_item_id, claim.worker_id, claim.lease_token)
            await conn.execute("UPDATE work_items SET lease_owner=NULL,lease_expires_at=NULL,last_heartbeat_at=NULL WHERE id=$1 AND lease_owner=$2 AND lease_token=$3", claim.work_item_id, claim.worker_id, claim.lease_token)
            await conn.execute("UPDATE goal_runs SET status=$1,finalization_state=$2,result_artifact_id=$3,revision=revision+1,updated_at=$4 WHERE id=$5", final_status, "partial_completed" if final_status == "partial_completed" else final_status, result_artifact_id, now, claim.goal_run_id)
            event_type = "finalization.partial_completed" if final_status == "partial_completed" else "finalization.completed"
            await self._pg_event(conn, aggregate_type="goal_run", aggregate_id=claim.goal_run_id, goal_run_id=claim.goal_run_id, event_type=event_type, payload={"snapshot_hash": snapshot_hash, "result_hash": result_hash, "result_artifact_id": result_artifact_id, "resumed": resumed, "status": final_status}, idempotency_key=f"finalization-result:{claim.goal_run_id}:{snapshot_hash}:{result_hash}")
            return {"status": "committed", "final_status": final_status, "result_hash": result_hash, "result_artifact_id": result_artifact_id}

        return await self._pg_tx(op_pg)

    async def create_fanin_snapshot(self, goal_run_id: str, *, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        """Persist an immutable ordered snapshot used by finalization."""
        now = time.time()

        def op(conn: Any) -> dict[str, Any]:
            rows = conn.execute("SELECT id,logical_key,state,result_hash,result_json,checkpoint_id,recovery_state FROM work_items WHERE goal_run_id=? ORDER BY created_at,id", (goal_run_id,)).fetchall()
            value = snapshot or {"goal_run_id": goal_run_id, "items": [dict(row) for row in rows]}
            encoded = canonical_json(value)
            digest = content_hash(value)
            existing = conn.execute("SELECT * FROM artifact_manifests WHERE goal_run_id=? AND manifest_hash=?", (goal_run_id, digest)).fetchone()
            if existing:
                return dict(existing)
            version = int(conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM artifact_manifests WHERE goal_run_id=?", (goal_run_id,)).fetchone()[0])
            manifest_id = new_id()
            conn.execute("INSERT INTO artifact_manifests(id,goal_run_id,version,manifest_hash,artifact_json,created_at) VALUES(?,?,?,?,?,?)", (manifest_id, goal_run_id, version, digest, encoded, now))
            self._sqlite_event(conn, aggregate_type="goal_run", aggregate_id=goal_run_id, goal_run_id=goal_run_id, event_type="fanin.snapshot_created", payload={"snapshot_id": manifest_id, "snapshot_hash": digest, "included_sequence": version}, idempotency_key=f"fanin-snapshot:{goal_run_id}:{digest}")
            return dict(conn.execute("SELECT * FROM artifact_manifests WHERE id=?", (manifest_id,)).fetchone())

        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, op)

        async def op_pg(conn: Any) -> dict[str, Any]:
            rows = await conn.fetch("SELECT id,logical_key,state,result_hash,result_json,checkpoint_id,recovery_state FROM work_items WHERE goal_run_id=$1 ORDER BY created_at,id", goal_run_id)
            value = snapshot or {"goal_run_id": goal_run_id, "items": [dict(row) for row in rows]}
            encoded = canonical_json(value)
            digest = content_hash(value)
            existing = await conn.fetchrow("SELECT * FROM artifact_manifests WHERE goal_run_id=$1 AND manifest_hash=$2", goal_run_id, digest)
            if existing:
                return dict(existing)
            version = int(await conn.fetchval("SELECT COALESCE(MAX(version),0)+1 FROM artifact_manifests WHERE goal_run_id=$1", goal_run_id))
            manifest_id = new_id()
            await conn.execute("INSERT INTO artifact_manifests(id,goal_run_id,version,manifest_hash,artifact_json,created_at) VALUES($1,$2,$3,$4,$5,$6)", manifest_id, goal_run_id, version, digest, encoded, now)
            await self._pg_event(conn, aggregate_type="goal_run", aggregate_id=goal_run_id, goal_run_id=goal_run_id, event_type="fanin.snapshot_created", payload={"snapshot_id": manifest_id, "snapshot_hash": digest, "included_sequence": version}, idempotency_key=f"fanin-snapshot:{goal_run_id}:{digest}")
            return dict(await conn.fetchrow("SELECT * FROM artifact_manifests WHERE id=$1", manifest_id))

        return await self._pg_tx(op_pg)

    async def checkpoint_finalization(
        self,
        claim: ClaimEnvelope,
        *,
        snapshot_hash: str,
        stage: str,
        output_hash: str | None = None,
        included_child_sequence: int = 0,
        checkpoint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = checkpoint or {}
        checkpoint_id = new_id()
        now = time.time()

        def op(conn: Any) -> dict[str, Any]:
            self._sqlite_fence(conn, claim)
            existing = conn.execute("SELECT * FROM finalization_checkpoints WHERE goal_run_id=? AND snapshot_hash=? AND stage=?", (claim.goal_run_id, snapshot_hash, stage)).fetchone()
            if existing:
                return dict(existing)
            conn.execute("INSERT INTO finalization_checkpoints(id,goal_run_id,work_item_id,snapshot_hash,stage,output_hash,included_child_sequence,checkpoint_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (checkpoint_id, claim.goal_run_id, claim.work_item_id, snapshot_hash, stage, output_hash, included_child_sequence, canonical_json(data), now))
            conn.execute("UPDATE goal_runs SET finalization_state='checkpointed',revision=revision+1,updated_at=? WHERE id=?", (now, claim.goal_run_id))
            self._sqlite_event(conn, aggregate_type="work_item", aggregate_id=claim.work_item_id, goal_run_id=claim.goal_run_id, event_type="work_item.checkpointed", payload={"checkpoint_id": checkpoint_id, "snapshot_hash": snapshot_hash, "stage": stage, "output_hash": output_hash}, idempotency_key=f"finalization-checkpoint:{claim.goal_run_id}:{snapshot_hash}:{stage}")
            return dict(conn.execute("SELECT * FROM finalization_checkpoints WHERE id=?", (checkpoint_id,)).fetchone())

        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, op)

        async def op_pg(conn: Any) -> dict[str, Any]:
            lease = await conn.fetchrow("SELECT 1 FROM execution_leases WHERE work_item_id=$1 AND owner_id=$2 AND token=$3 AND expires_at>$4 AND released_at IS NULL", claim.work_item_id, claim.worker_id, claim.lease_token, now)
            if lease is None:
                raise DurableExecutionError("STALE_FENCE", "finalization checkpoint rejected")
            existing = await conn.fetchrow("SELECT * FROM finalization_checkpoints WHERE goal_run_id=$1 AND snapshot_hash=$2 AND stage=$3", claim.goal_run_id, snapshot_hash, stage)
            if existing:
                return dict(existing)
            await conn.execute("INSERT INTO finalization_checkpoints(id,goal_run_id,work_item_id,snapshot_hash,stage,output_hash,included_child_sequence,checkpoint_json,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)", checkpoint_id, claim.goal_run_id, claim.work_item_id, snapshot_hash, stage, output_hash, included_child_sequence, canonical_json(data), now)
            await conn.execute("UPDATE goal_runs SET finalization_state='checkpointed',revision=revision+1,updated_at=$1 WHERE id=$2", now, claim.goal_run_id)
            await self._pg_event(conn, aggregate_type="work_item", aggregate_id=claim.work_item_id, goal_run_id=claim.goal_run_id, event_type="work_item.checkpointed", payload={"checkpoint_id": checkpoint_id, "snapshot_hash": snapshot_hash, "stage": stage, "output_hash": output_hash}, idempotency_key=f"finalization-checkpoint:{claim.goal_run_id}:{snapshot_hash}:{stage}")
            return dict(await conn.fetchrow("SELECT * FROM finalization_checkpoints WHERE id=$1", checkpoint_id))

        return await self._pg_tx(op_pg)

    async def list_finalization_checkpoints(self, goal_run_id: str) -> list[dict[str, Any]]:
        """Return immutable finalization checkpoints in creation order."""
        def op(conn: Any) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT * FROM finalization_checkpoints WHERE goal_run_id=? ORDER BY created_at,id",
                (goal_run_id,),
            ).fetchall()
            return [dict(row) for row in rows]

        if self.backend == "sqlite":
            return await asyncio.to_thread(lambda: self._sqlite_read(op))

        async def op_pg(conn: Any) -> list[dict[str, Any]]:
            rows = await conn.fetch(
                "SELECT * FROM finalization_checkpoints WHERE goal_run_id=$1 ORDER BY created_at,id",
                goal_run_id,
            )
            return [dict(row) for row in rows]

        return await self._pg_tx(op_pg)

    async def repair_terminal_children(self, goal_run_id: str | None = None) -> int:
        """Close never-started children left by an interrupted finalization.

        Terminal GoalRuns cannot accept more work. This repair is deliberately
        additive: it preserves the child row and appends a cancellation event
        instead of deleting or silently discarding the pending work.
        """
        def op(conn: Any) -> int:
            clauses = [
                "gr.status IN ('completed','partial_completed','failed','cancelled','blocked')",
                "wi.kind <> 'finalize'",
                "wi.state IN ('created','ready','retry_wait')",
            ]
            params: list[Any] = []
            if goal_run_id:
                clauses.append("wi.goal_run_id=?")
                params.append(goal_run_id)
            rows = conn.execute(
                "SELECT wi.id,wi.goal_run_id,wi.logical_key FROM work_items wi "
                "JOIN goal_runs gr ON gr.id=wi.goal_run_id WHERE " + " AND ".join(clauses),
                params,
            ).fetchall()
            now = time.time()
            for row in rows:
                conn.execute(
                    "UPDATE work_items SET state='cancelled',recovery_state='classified',error_json=?,revision=revision+1,updated_at=? WHERE id=? AND state IN ('created','ready','retry_wait')",
                    (canonical_json({"message": "terminal GoalRun repaired pending child"}), now, row["id"]),
                )
                self._sqlite_event(
                    conn,
                    aggregate_type="work_item",
                    aggregate_id=row["id"],
                    goal_run_id=row["goal_run_id"],
                    event_type="work_item.cancelled",
                    payload={"source": "reconciler", "reason": "terminal_goal_repair", "logical_key": row["logical_key"]},
                    idempotency_key=f"terminal-child-repair:{row['id']}",
                )
            return len(rows)

        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, op)

        async def op_pg(conn: Any) -> int:
            clauses = [
                "gr.status IN ('completed','partial_completed','failed','cancelled','blocked')",
                "wi.kind <> 'finalize'",
                "wi.state IN ('created','ready','retry_wait')",
            ]
            params: list[Any] = []
            if goal_run_id:
                clauses.append(f"wi.goal_run_id=${len(params)+1}")
                params.append(goal_run_id)
            rows = await conn.fetch(
                "SELECT wi.id,wi.goal_run_id,wi.logical_key FROM work_items wi "
                "JOIN goal_runs gr ON gr.id=wi.goal_run_id WHERE " + " AND ".join(clauses) + " FOR UPDATE",
                *params,
            )
            now = time.time()
            for row in rows:
                await conn.execute(
                    "UPDATE work_items SET state='cancelled',recovery_state='classified',error_json=$1,revision=revision+1,updated_at=$2 WHERE id=$3 AND state IN ('created','ready','retry_wait')",
                    canonical_json({"message": "terminal GoalRun repaired pending child"}),
                    now,
                    row["id"],
                )
                await self._pg_event(
                    conn,
                    aggregate_type="work_item",
                    aggregate_id=row["id"],
                    goal_run_id=row["goal_run_id"],
                    event_type="work_item.cancelled",
                    payload={"source": "reconciler", "reason": "terminal_goal_repair", "logical_key": row["logical_key"]},
                    idempotency_key=f"terminal-child-repair:{row['id']}",
                )
            return len(rows)

        return await self._pg_tx(op_pg)

    def _reconcile_one_sqlite(self, conn: Any, lease: Any, now: float, actor: str) -> dict[str, Any]:
        ready_at = time.time()
        item = conn.execute("SELECT * FROM work_items WHERE id=?", (lease["work_item_id"],)).fetchone()
        if item is None:
            return {"decision": "MANUAL_REVIEW", "work_item_id": lease["work_item_id"]}
        attempt = conn.execute("SELECT * FROM work_attempts WHERE work_item_id=? AND lease_token=? ORDER BY attempt_no DESC LIMIT 1", (item["id"], lease["token"])).fetchone()
        attempt_id = attempt["id"] if attempt else None
        if item["state"] == "succeeded" and item["result_json"]:
            decision = "COMPLETED_FROM_EVIDENCE"
            state = "succeeded"
            recovered = 1
        else:
            effect = conn.execute("SELECT * FROM side_effects WHERE work_item_id=? AND state IN ('started','unknown','committed','manual_review') ORDER BY first_seen_at DESC LIMIT 1", (item["id"],)).fetchone()
            if effect is not None and effect["state"] == "manual_review":
                decision = "MANUAL_REVIEW"
                state = "quarantined_unknown"
                recovered = 0
            elif effect is not None and (effect["state"] == "committed" or _decode(effect["probe_result_json"], {}).get("status") in {"committed", "succeeded"}):
                decision = "COMPLETED_FROM_EVIDENCE"
                state = "succeeded"
                recovered = 1
                if not item["result_json"]:
                    recovered_result = {"recovered_from_side_effect": True, "operation_key": effect["operation_key"], "provider_request_id": effect["provider_request_id"]}
                    result_json = canonical_json(recovered_result)
                    result_hash = content_hash(recovered_result)
                    conn.execute("UPDATE work_items SET result_json=?,result_hash=? WHERE id=?", (result_json, result_hash, item["id"]))
            elif effect is None or item["side_effect_policy"] in {"none", "idempotent"}:
                decision = "IDEMPOTENT_RETRY" if item["side_effect_policy"] == "idempotent" else "RETRY_SAFE"
                state = "retry_wait"
                recovered = 0
            elif _decode(effect["probe_result_json"], {}).get("status") in {"not_found", "not_started"} and item["side_effect_policy"] == "probe_required":
                decision = "RETRY_SAFE"
                state = "retry_wait"
                recovered = 0
            else:
                decision = "QUARANTINED_UNKNOWN" if item["side_effect_policy"] == "probe_required" else "MANUAL_REVIEW"
                state = "quarantined_unknown"
                recovered = 0
        conn.execute("UPDATE execution_leases SET released_at=?,release_reason=? WHERE work_item_id=? AND released_at IS NULL", (now, decision, item["id"]))
        conn.execute("UPDATE work_items SET state=?,recovery_state=?,lease_owner=NULL,lease_expires_at=NULL,last_heartbeat_at=NULL,next_ready_at=?,revision=revision+1,updated_at=? WHERE id=?", (state, "classified" if state != "quarantined_unknown" else "quarantined", ready_at, now, item["id"]))
        if state == "succeeded":
            for promoted in self._dependencies_advance_sqlite(conn, item["goal_run_id"], item["id"]):
                self._sqlite_event(conn, aggregate_type="work_item", aggregate_id=promoted, goal_run_id=item["goal_run_id"], event_type="work_item.ready", payload={"reason": "recovery_completed", "completed_work_item_id": item["id"]}, idempotency_key=f"ready:recovery:{promoted}:{item['id']}")
        if attempt:
            attempt_state = "succeeded" if state == "succeeded" else "heartbeat_expired" if state == "retry_wait" else "unknown"
            conn.execute("UPDATE work_attempts SET state=?,ended_at=?,unknown_reason=? WHERE id=?", (attempt_state, now, decision if attempt_state == "unknown" else None, attempt_id))
        evidence = {"lease_owner": lease["owner_id"], "lease_token": lease["token"], "expires_at": lease["expires_at"]}
        if lease["released_at"] is None:
            self._sqlite_event(
                conn,
                aggregate_type="work_item",
                aggregate_id=item["id"],
                goal_run_id=item["goal_run_id"],
                event_type="lease.expired",
                payload=evidence,
                idempotency_key=f"lease-expired:{item['id']}:{lease['token']}",
            )
        decision_id = new_id()
        conn.execute("INSERT OR IGNORE INTO recovery_decisions(id,goal_run_id,work_item_id,attempt_id,observed_state,evidence_json,decision,policy_version,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (decision_id, item["goal_run_id"], item["id"], attempt_id, item["state"], canonical_json(evidence), decision, self.policy_version, actor, now))
        self._sqlite_event(conn, aggregate_type="work_item", aggregate_id=item["id"], goal_run_id=item["goal_run_id"], event_type="recovery.decision", payload={"decision": decision, "attempt_id": attempt_id, "evidence": evidence, "policy_version": self.policy_version}, idempotency_key=f"recovery:{item['id']}:{lease['token']}:{decision}")
        return {"decision": decision, "work_item_id": item["id"], "attempt_id": attempt_id, "recovered": recovered}

    async def reconcile(
        self,
        scope: str | None = None,
        *,
        cursor: str | None = None,
        limit: int = 100,
        actor: str = "reconciler",
        now: float | None = None,
    ) -> ReconciliationReport:
        """Classify expired leases; repeated scans are safe after release."""
        del cursor
        scan_time = now if now is not None else time.time()
        await self.repair_terminal_children(scope)

        def op(conn: Any) -> ReconciliationReport:
            clauses = [
                "((execution_leases.expires_at < ? AND execution_leases.released_at IS NULL)"
                " OR (work_items.state='unknown' AND work_items.recovery_state='pending'"
                " AND execution_leases.released_at IS NOT NULL))"
            ]
            params: list[Any] = [scan_time]
            if scope:
                clauses.append("execution_leases.work_item_id IN (SELECT id FROM work_items WHERE goal_run_id=?)")
                params.append(scope)
            recovery_id = scope or "runtime"
            self._sqlite_event(
                conn,
                aggregate_type="recovery",
                aggregate_id=recovery_id,
                goal_run_id=scope or recovery_id,
                event_type="recovery.started",
                payload={"scope": scope, "limit": limit},
                idempotency_key=f"recovery-started:{recovery_id}:{int(scan_time * 1000)}",
            )
            leases = conn.execute(
                "SELECT execution_leases.* FROM execution_leases JOIN work_items "
                "ON work_items.id=execution_leases.work_item_id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY execution_leases.expires_at LIMIT ?",
                (*params, max(1, limit)),
            ).fetchall()
            report = ReconciliationReport(scanned=len(leases))
            for lease in leases:
                decision = self._reconcile_one_sqlite(conn, lease, scan_time, actor)
                report.decisions.append(decision)
                if decision["decision"] in {"RETRY_SAFE", "IDEMPOTENT_RETRY"}:
                    report.retry_safe += 1
                elif decision["decision"] == "QUARANTINED_UNKNOWN":
                    report.quarantined += 1
                elif decision["decision"] == "MANUAL_REVIEW":
                    report.manual_review += 1
                elif decision["decision"] == "COMPLETED_FROM_EVIDENCE":
                    report.completed_from_evidence += 1
            self._sqlite_event(
                conn,
                aggregate_type="recovery",
                aggregate_id=recovery_id,
                goal_run_id=scope or recovery_id,
                event_type="recovery.completed",
                payload=report.to_dict(),
                idempotency_key=f"recovery-completed:{recovery_id}:{int(scan_time * 1000)}",
            )
            return report

        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, op)

        async def op_pg(conn: Any) -> ReconciliationReport:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"veya:reconcile:{scope or 'runtime'}")
            clauses = [
                "((execution_leases.expires_at < $1 AND execution_leases.released_at IS NULL)"
                " OR (work_items.state='unknown' AND work_items.recovery_state='pending'"
                " AND execution_leases.released_at IS NOT NULL))"
            ]
            params: list[Any] = [scan_time]
            if scope:
                clauses.append(f"execution_leases.work_item_id IN (SELECT id FROM work_items WHERE goal_run_id=${len(params)+1})")
                params.append(scope)
            recovery_id = scope or "runtime"
            await self._pg_event(conn, aggregate_type="recovery", aggregate_id=recovery_id, goal_run_id=scope or recovery_id, event_type="recovery.started", payload={"scope": scope, "limit": limit}, idempotency_key=f"recovery-started:{recovery_id}:{int(scan_time * 1000)}")
            leases = await conn.fetch(
                "SELECT execution_leases.* FROM execution_leases JOIN work_items "
                "ON work_items.id=execution_leases.work_item_id WHERE "
                + " AND ".join(clauses)
                + f" ORDER BY execution_leases.expires_at LIMIT ${len(params)+1} FOR UPDATE SKIP LOCKED",
                *params,
                max(1, limit),
            )
            report = ReconciliationReport(scanned=len(leases))
            # Use the same decision rules; each item is locked before mutation.
            for lease in leases:
                item = await conn.fetchrow("SELECT * FROM work_items WHERE id=$1 FOR UPDATE", lease["work_item_id"])
                if item is None:
                    continue
                attempt = await conn.fetchrow("SELECT * FROM work_attempts WHERE work_item_id=$1 AND lease_token=$2 ORDER BY attempt_no DESC LIMIT 1", item["id"], lease["token"])
                effect = await conn.fetchrow("SELECT * FROM side_effects WHERE work_item_id=$1 AND state IN ('started','unknown','committed','manual_review') ORDER BY first_seen_at DESC LIMIT 1", item["id"])
                if item["state"] == "succeeded" and item["result_json"]:
                    decision, state, recovered = "COMPLETED_FROM_EVIDENCE", "succeeded", 1
                elif effect is not None and effect["state"] == "manual_review":
                    decision, state, recovered = "MANUAL_REVIEW", "quarantined_unknown", 0
                elif effect is not None and (effect["state"] == "committed" or _decode(effect["probe_result_json"], {}).get("status") in {"committed", "succeeded"}):
                    decision, state, recovered = "COMPLETED_FROM_EVIDENCE", "succeeded", 1
                    if not item["result_json"]:
                        recovered_result = {"recovered_from_side_effect": True, "operation_key": effect["operation_key"], "provider_request_id": effect["provider_request_id"]}
                        await conn.execute(
                            "UPDATE work_items SET result_json=$1,result_hash=$2,revision=revision+1,updated_at=$3 WHERE id=$4",
                            canonical_json(recovered_result), content_hash(recovered_result), scan_time, item["id"],
                        )
                elif effect is None or item["side_effect_policy"] in {"none", "idempotent"}:
                    decision, state, recovered = ("IDEMPOTENT_RETRY" if item["side_effect_policy"] == "idempotent" else "RETRY_SAFE"), "retry_wait", 0
                elif _decode(effect["probe_result_json"], {}).get("status") in {"not_found", "not_started"} and item["side_effect_policy"] == "probe_required":
                    decision, state, recovered = "RETRY_SAFE", "retry_wait", 0
                else:
                    decision, state, recovered = ("QUARANTINED_UNKNOWN" if item["side_effect_policy"] == "probe_required" else "MANUAL_REVIEW"), "quarantined_unknown", 0
                await conn.execute("UPDATE execution_leases SET released_at=$1,release_reason=$2 WHERE work_item_id=$3 AND released_at IS NULL", scan_time, decision, item["id"])
                await conn.execute("UPDATE work_items SET state=$1,recovery_state=$2,lease_owner=NULL,lease_expires_at=NULL,last_heartbeat_at=NULL,next_ready_at=$3,revision=revision+1,updated_at=$4 WHERE id=$5", state, "classified" if state != "quarantined_unknown" else "quarantined", time.time(), scan_time, item["id"])
                if state == "succeeded":
                    dependent_rows = await conn.fetch("SELECT * FROM work_items WHERE goal_run_id=$1 AND state='created'", item["goal_run_id"])
                    for dependent in dependent_rows:
                        deps = _decode(dependent["dependency_json"], [])
                        if not deps:
                            continue
                        dep_rows = await conn.fetch("SELECT logical_key,state FROM work_items WHERE goal_run_id=$1 AND logical_key=ANY($2::text[])", item["goal_run_id"], list(deps))
                        if all({dep_row["logical_key"]: dep_row["state"] for dep_row in dep_rows}.get(dep) == "succeeded" for dep in deps):
                            await conn.execute("UPDATE work_items SET state='ready',revision=revision+1,updated_at=$1 WHERE id=$2 AND state='created'", scan_time, dependent["id"])
                            await self._pg_event(conn, aggregate_type="work_item", aggregate_id=dependent["id"], goal_run_id=item["goal_run_id"], event_type="work_item.ready", payload={"reason": "recovery_completed", "completed_work_item_id": item["id"]}, idempotency_key=f"ready:recovery:{dependent['id']}:{item['id']}")
                if attempt:
                    await conn.execute("UPDATE work_attempts SET state=$1,ended_at=$2,unknown_reason=$3 WHERE id=$4", "succeeded" if state == "succeeded" else "heartbeat_expired" if state == "retry_wait" else "unknown", scan_time, decision if state == "quarantined_unknown" else None, attempt["id"])
                decision_id = new_id()
                await conn.execute("INSERT INTO recovery_decisions(id,goal_run_id,work_item_id,attempt_id,observed_state,evidence_json,decision,policy_version,actor,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT DO NOTHING", decision_id, item["goal_run_id"], item["id"], attempt["id"] if attempt else None, item["state"], canonical_json({"lease_owner": lease["owner_id"], "lease_token": lease["token"], "expires_at": lease["expires_at"]}), decision, self.policy_version, actor, scan_time)
                if lease["released_at"] is None:
                    await self._pg_event(conn, aggregate_type="work_item", aggregate_id=item["id"], goal_run_id=item["goal_run_id"], event_type="lease.expired", payload={"lease_owner": lease["owner_id"], "lease_token": lease["token"], "expires_at": lease["expires_at"]}, idempotency_key=f"lease-expired:{item['id']}:{lease['token']}")
                report.decisions.append({"decision": decision, "work_item_id": item["id"], "attempt_id": attempt["id"] if attempt else None, "recovered": recovered})
                if decision in {"RETRY_SAFE", "IDEMPOTENT_RETRY"}:
                    report.retry_safe += 1
                elif decision == "QUARANTINED_UNKNOWN":
                    report.quarantined += 1
                elif decision == "MANUAL_REVIEW":
                    report.manual_review += 1
                elif decision == "COMPLETED_FROM_EVIDENCE":
                    report.completed_from_evidence += 1
            await self._pg_event(conn, aggregate_type="recovery", aggregate_id=recovery_id, goal_run_id=scope or recovery_id, event_type="recovery.completed", payload=report.to_dict(), idempotency_key=f"recovery-completed:{recovery_id}:{int(scan_time * 1000)}")
            return report

        return await self._pg_tx(op_pg)

    async def list_outbox(self, *, limit: int = 100, now: float | None = None) -> list[OutboxMessage]:
        current = now if now is not None else time.time()

        def op(conn: Any) -> list[OutboxMessage]:
            rows = conn.execute("SELECT o.event_id,o.destination,o.publish_attempts,e.event_json FROM execution_outbox o JOIN execution_events e ON e.id=o.event_id WHERE o.published_at IS NULL AND o.next_attempt_at<=? ORDER BY o.next_attempt_at,e.occurred_at LIMIT ?", (current, max(1, limit))).fetchall()
            return [OutboxMessage(row["event_id"], _decode(row["event_json"], {}), row["destination"], row["publish_attempts"]) for row in rows]

        if self.backend == "sqlite":
            return await asyncio.to_thread(lambda: self._sqlite_read(op))

        async def op_pg(conn: Any) -> list[OutboxMessage]:
            rows = await conn.fetch("SELECT o.event_id,o.destination,o.publish_attempts,e.event_json FROM execution_outbox o JOIN execution_events e ON e.id=o.event_id WHERE o.published_at IS NULL AND o.next_attempt_at<=$1 ORDER BY o.next_attempt_at,e.occurred_at LIMIT $2", current, max(1, limit))
            return [OutboxMessage(row["event_id"], _decode(row["event_json"], {}), row["destination"], row["publish_attempts"]) for row in rows]

        return await self._pg_tx(op_pg)

    def _sqlite_read(self, fn: Callable[[Any], Any]) -> Any:
        self._sqlite_prepare()
        with self._sqlite_lock:
            conn = self._sqlite_connection()
            try:
                return fn(conn)
            finally:
                if conn is not self._sqlite_memory:
                    conn.close()

    async def mark_outbox(self, event_id: str, *, published: bool, error: str | None = None) -> None:
        now = time.time()

        def op(conn: Any) -> None:
            if published:
                conn.execute("UPDATE execution_outbox SET published_at=?,publish_attempts=publish_attempts+1,last_error=NULL WHERE event_id=?", (now, event_id))
            else:
                conn.execute("UPDATE execution_outbox SET publish_attempts=publish_attempts+1,last_error=?,next_attempt_at=? WHERE event_id=?", (str(error or "publish failed")[:500], now + 1.0, event_id))

        if self.backend == "sqlite":
            await asyncio.to_thread(self._sqlite_tx, op)
            return

        async def op_pg(conn: Any) -> None:
            if published:
                await conn.execute("UPDATE execution_outbox SET published_at=$1,publish_attempts=publish_attempts+1,last_error=NULL WHERE event_id=$2", now, event_id)
            else:
                await conn.execute("UPDATE execution_outbox SET publish_attempts=publish_attempts+1,last_error=$1,next_attempt_at=$2 WHERE event_id=$3", str(error or "publish failed")[:500], now + 1.0, event_id)

        await self._pg_tx(op_pg)

    async def publish_outbox(self, publisher: Callable[[dict[str, Any]], Awaitable[None] | None], *, limit: int = 100) -> dict[str, int]:
        messages = await self.list_outbox(limit=limit)
        published = 0
        failed = 0
        for message in messages:
            try:
                result = publisher(message.event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                failed += 1
                await self.mark_outbox(message.event_id, published=False, error=str(exc))
            else:
                published += 1
                await self.mark_outbox(message.event_id, published=True)
        return {"published": published, "failed": failed}

    async def list_events(self, goal_run_id: str, *, after_sequence: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        def op(conn: Any) -> list[dict[str, Any]]:
            rows = conn.execute("SELECT event_json FROM execution_events WHERE goal_run_id=? AND sequence_no>? ORDER BY sequence_no LIMIT ?", (goal_run_id, after_sequence, max(1, limit))).fetchall()
            return [_decode(row["event_json"], {}) for row in rows]

        if self.backend == "sqlite":
            return await asyncio.to_thread(lambda: self._sqlite_read(op))

        async def op_pg(conn: Any) -> list[dict[str, Any]]:
            rows = await conn.fetch("SELECT event_json FROM execution_events WHERE goal_run_id=$1 AND sequence_no>$2 ORDER BY sequence_no LIMIT $3", goal_run_id, after_sequence, max(1, limit))
            return [_decode(row["event_json"], {}) for row in rows]

        return await self._pg_tx(op_pg)

    async def get_goal_run(self, goal_run_id: str) -> dict[str, Any] | None:
        def op(conn: Any) -> dict[str, Any] | None:
            row = conn.execute("SELECT * FROM goal_runs WHERE id=?", (goal_run_id,)).fetchone()
            if row is None:
                return None
            value = dict(row)
            value["budget"] = _decode(value.pop("budget_json"), {})
            value["acceptance"] = _decode(value.pop("acceptance_json"), [])
            return value

        if self.backend == "sqlite":
            return await asyncio.to_thread(lambda: self._sqlite_read(op))

        async def op_pg(conn: Any) -> dict[str, Any] | None:
            row = await conn.fetchrow("SELECT * FROM goal_runs WHERE id=$1", goal_run_id)
            if row is None:
                return None
            value = dict(row)
            value["budget"] = _decode(value.pop("budget_json"), {})
            value["acceptance"] = _decode(value.pop("acceptance_json"), [])
            return value

        return await self._pg_tx(op_pg)

    async def list_work_items(self, goal_run_id: str) -> list[dict[str, Any]]:
        """Return the durable work projection in stable creation order."""

        def op(conn: Any) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT * FROM work_items WHERE goal_run_id=? ORDER BY created_at,id",
                (goal_run_id,),
            ).fetchall()
            return [dict(row) for row in rows]

        if self.backend == "sqlite":
            return await asyncio.to_thread(lambda: self._sqlite_read(op))

        async def op_pg(conn: Any) -> list[dict[str, Any]]:
            rows = await conn.fetch(
                "SELECT * FROM work_items WHERE goal_run_id=$1 ORDER BY created_at,id",
                goal_run_id,
            )
            return [dict(row) for row in rows]

        return await self._pg_tx(op_pg)

    async def list_attempts(self, work_item_id: str) -> list[dict[str, Any]]:
        def op(conn: Any) -> list[dict[str, Any]]:
            return [dict(row) for row in conn.execute("SELECT * FROM work_attempts WHERE work_item_id=? ORDER BY attempt_no", (work_item_id,)).fetchall()]

        if self.backend == "sqlite":
            return await asyncio.to_thread(lambda: self._sqlite_read(op))

        async def op_pg(conn: Any) -> list[dict[str, Any]]:
            return [dict(row) for row in await conn.fetch("SELECT * FROM work_attempts WHERE work_item_id=$1 ORDER BY attempt_no", work_item_id)]

        return await self._pg_tx(op_pg)

    async def register_artifact(
        self,
        *,
        goal_run_id: str,
        content_uri: str,
        content_hash_value: str,
        size_bytes: int,
        mime_type: str,
        kind: str,
        work_item_id: str | None = None,
        visibility: str = "internal",
        claim: ClaimEnvelope | None = None,
    ) -> dict[str, Any]:
        """Register immutable artifact metadata; bytes remain in ArtifactStore."""
        if not content_hash_value.startswith("sha256:") or size_bytes < 0:
            raise DurableExecutionError("INVALID_ARTIFACT", "hash or size is invalid")
        now = time.time()

        def op(conn: Any) -> dict[str, Any]:
            if claim is not None:
                if claim.goal_run_id != goal_run_id or claim.work_item_id != work_item_id:
                    raise DurableExecutionError("STALE_FENCE", "artifact claim does not match work item")
                self._sqlite_fence(conn, claim)
            existing_uri = conn.execute("SELECT * FROM artifacts WHERE goal_run_id=? AND content_uri=?", (goal_run_id, content_uri)).fetchone()
            if existing_uri:
                if existing_uri["content_hash"] != content_hash_value:
                    raise DurableExecutionError("ARTIFACT_CONFLICT", "immutable URI has a different hash")
                return dict(existing_uri)
            artifact_id = new_id()
            conn.execute("INSERT INTO artifacts(id,goal_run_id,work_item_id,kind,content_uri,content_hash,size_bytes,mime_type,visibility,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (artifact_id, goal_run_id, work_item_id, kind, content_uri, content_hash_value, size_bytes, mime_type, visibility, now))
            self._sqlite_event(conn, aggregate_type="artifact", aggregate_id=artifact_id, goal_run_id=goal_run_id, event_type="artifact.created", payload={"artifact_id": artifact_id, "content_uri": content_uri, "content_hash": content_hash_value, "kind": kind, "visibility": visibility}, idempotency_key=f"artifact-created:{artifact_id}")
            return dict(conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone())

        if self.backend == "sqlite":
            if claim is None:
                return await asyncio.to_thread(self._sqlite_tx, op)
            return await self._guard_fenced(
                claim, "artifact", lambda: asyncio.to_thread(self._sqlite_tx, op)
            )

        async def op_pg(conn: Any) -> dict[str, Any]:
            if claim is not None:
                if claim.goal_run_id != goal_run_id or claim.work_item_id != work_item_id:
                    raise DurableExecutionError("STALE_FENCE", "artifact claim does not match work item")
                lease = await conn.fetchrow("SELECT 1 FROM execution_leases WHERE work_item_id=$1 AND owner_id=$2 AND token=$3 AND expires_at>$4 AND released_at IS NULL", claim.work_item_id, claim.worker_id, claim.lease_token, now)
                if lease is None:
                    raise DurableExecutionError("STALE_FENCE", "artifact claim is no longer current")
            existing_uri = await conn.fetchrow("SELECT * FROM artifacts WHERE goal_run_id=$1 AND content_uri=$2", goal_run_id, content_uri)
            if existing_uri:
                if existing_uri["content_hash"] != content_hash_value:
                    raise DurableExecutionError("ARTIFACT_CONFLICT", "immutable URI has a different hash")
                return dict(existing_uri)
            artifact_id = new_id()
            await conn.execute("INSERT INTO artifacts(id,goal_run_id,work_item_id,kind,content_uri,content_hash,size_bytes,mime_type,visibility,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)", artifact_id, goal_run_id, work_item_id, kind, content_uri, content_hash_value, size_bytes, mime_type, visibility, now)
            await self._pg_event(conn, aggregate_type="artifact", aggregate_id=artifact_id, goal_run_id=goal_run_id, event_type="artifact.created", payload={"artifact_id": artifact_id, "content_uri": content_uri, "content_hash": content_hash_value, "kind": kind, "visibility": visibility}, idempotency_key=f"artifact-created:{artifact_id}")
            return dict(await conn.fetchrow("SELECT * FROM artifacts WHERE id=$1", artifact_id))

        if claim is None:
            return await self._pg_tx(op_pg)
        return await self._guard_fenced(claim, "artifact", lambda: self._pg_tx(op_pg))

    async def record_migration(
        self,
        *,
        flag: str,
        phase: str,
        cohort: str,
        operator: str,
        ended: bool = False,
        rollback_marker: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        migration_id = new_id()

        def op(conn: Any) -> dict[str, Any]:
            conn.execute("INSERT INTO runtime_migrations(id,flag,phase,cohort,started_at,ended_at,operator,rollback_marker) VALUES(?,?,?,?,?,?,?,?)", (migration_id, flag, phase, cohort, now, now if ended else None, operator, rollback_marker))
            return dict(conn.execute("SELECT * FROM runtime_migrations WHERE id=?", (migration_id,)).fetchone())

        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, op)

        async def op_pg(conn: Any) -> dict[str, Any]:
            await conn.execute("INSERT INTO runtime_migrations(id,flag,phase,cohort,started_at,ended_at,operator,rollback_marker) VALUES($1,$2,$3,$4,$5,$6,$7,$8)", migration_id, flag, phase, cohort, now, now if ended else None, operator, rollback_marker)
            return dict(await conn.fetchrow("SELECT * FROM runtime_migrations WHERE id=$1", migration_id))

        return await self._pg_tx(op_pg)

    async def record_shadow_comparison(
        self,
        *,
        goal_run_id: str,
        legacy_hash: str,
        durable_hash: str,
        diff_class: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        comparison_id = new_id()
        now = time.time()

        def op(conn: Any) -> dict[str, Any]:
            conn.execute("INSERT INTO shadow_comparisons(id,goal_run_id,legacy_hash,durable_hash,diff_class,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (comparison_id, goal_run_id, legacy_hash, durable_hash, diff_class, canonical_json(payload or {}), now))
            return dict(conn.execute("SELECT * FROM shadow_comparisons WHERE id=?", (comparison_id,)).fetchone())

        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, op)

        async def op_pg(conn: Any) -> dict[str, Any]:
            await conn.execute("INSERT INTO shadow_comparisons(id,goal_run_id,legacy_hash,durable_hash,diff_class,payload_json,created_at) VALUES($1,$2,$3,$4,$5,$6,$7)", comparison_id, goal_run_id, legacy_hash, durable_hash, diff_class, canonical_json(payload or {}), now)
            return dict(await conn.fetchrow("SELECT * FROM shadow_comparisons WHERE id=$1", comparison_id))

        return await self._pg_tx(op_pg)

    async def record_dual_run_comparison(
        self,
        *,
        goal_run_id: str,
        authoritative_hash: str,
        shadow_hash: str,
        artifact_diff: str,
        classification: str,
        latency_ms: int = 0,
        usage: dict[str, Any] | None = None,
        reviewer_status: str = "pending",
    ) -> dict[str, Any]:
        comparison_id = new_id()
        now = time.time()

        def op(conn: Any) -> dict[str, Any]:
            conn.execute("INSERT INTO dual_run_comparisons(id,goal_run_id,authoritative_hash,shadow_hash,artifact_diff,latency_ms,usage_json,classification,reviewer_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (comparison_id, goal_run_id, authoritative_hash, shadow_hash, artifact_diff, max(0, latency_ms), canonical_json(usage or {}), classification, reviewer_status, now))
            return dict(conn.execute("SELECT * FROM dual_run_comparisons WHERE id=?", (comparison_id,)).fetchone())

        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, op)

        async def op_pg(conn: Any) -> dict[str, Any]:
            await conn.execute("INSERT INTO dual_run_comparisons(id,goal_run_id,authoritative_hash,shadow_hash,artifact_diff,latency_ms,usage_json,classification,reviewer_status,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)", comparison_id, goal_run_id, authoritative_hash, shadow_hash, artifact_diff, max(0, latency_ms), canonical_json(usage or {}), classification, reviewer_status, now)
            return dict(await conn.fetchrow("SELECT * FROM dual_run_comparisons WHERE id=$1", comparison_id))

        return await self._pg_tx(op_pg)

    async def health(self) -> dict[str, Any]:
        try:
            now = time.time()
            if self.backend == "sqlite":
                def read_health(conn: Any) -> dict[str, Any]:
                    version = conn.execute("SELECT version FROM execution_schema_meta ORDER BY version DESC LIMIT 1").fetchone()
                    return {
                        "schema_version": int(version[0] if version else 0),
                        "ready": int(conn.execute("SELECT COUNT(*) FROM work_items WHERE state IN ('ready','retry_wait')").fetchone()[0]),
                        "queue_depth": int(conn.execute("SELECT COUNT(*) FROM work_items WHERE state IN ('ready','retry_wait')").fetchone()[0]),
                        "active_leases": int(conn.execute("SELECT COUNT(*) FROM execution_leases WHERE released_at IS NULL AND expires_at>?", (now,)).fetchone()[0]),
                        "expired_leases": int(conn.execute("SELECT COUNT(*) FROM execution_leases WHERE released_at IS NULL AND expires_at<=?", (now,)).fetchone()[0]),
                        "outbox_pending": int(conn.execute("SELECT COUNT(*) FROM execution_outbox WHERE published_at IS NULL").fetchone()[0]),
                        "quarantined": int(conn.execute("SELECT COUNT(*) FROM work_items WHERE state='quarantined_unknown'").fetchone()[0]),
                        "quarantined_count": int(conn.execute("SELECT COUNT(*) FROM work_items WHERE state='quarantined_unknown'").fetchone()[0]),
                    }

                value = await asyncio.to_thread(lambda: self._sqlite_read(read_health))
            else:
                if self._pool is None:
                    raise DurableExecutionError("DATABASE_UNAVAILABLE", "repository is not connected")
                async with self._pool.acquire() as conn:
                    value = dict(await conn.fetchrow("SELECT version FROM execution_schema_meta ORDER BY version DESC LIMIT 1"))
                    value.update(
                        ready=int(await conn.fetchval("SELECT COUNT(*) FROM work_items WHERE state IN ('ready','retry_wait')")),
                        queue_depth=int(await conn.fetchval("SELECT COUNT(*) FROM work_items WHERE state IN ('ready','retry_wait')")),
                        active_leases=int(await conn.fetchval("SELECT COUNT(*) FROM execution_leases WHERE released_at IS NULL AND expires_at>$1", now)),
                        expired_leases=int(await conn.fetchval("SELECT COUNT(*) FROM execution_leases WHERE released_at IS NULL AND expires_at<=$1", now)),
                        outbox_pending=int(await conn.fetchval("SELECT COUNT(*) FROM execution_outbox WHERE published_at IS NULL")),
                        quarantined=int(await conn.fetchval("SELECT COUNT(*) FROM work_items WHERE state='quarantined_unknown'")),
                        quarantined_count=int(await conn.fetchval("SELECT COUNT(*) FROM work_items WHERE state='quarantined_unknown'")),
                    )
            return {
                "ok": bool(value),
                "backend": self.backend,
                "authority": "postgresql" if self.backend == "postgres" else "sqlite",
                "db_connected": True,
                "schema_version": int(value.get("version", 0)),
                "pending_outbox": int(value.get("outbox_pending", 0)),
                **value,
            }
        except Exception as exc:
            return {"ok": False, "backend": self.backend, "error": f"{type(exc).__name__}: {exc}"}

    async def metrics(self) -> dict[str, int | float]:
        """Return durable counters derived from the append-only audit state.

        These counters intentionally come from PostgreSQL rather than process
        memory, so multiple backend processes report one shared authority.
        Counters whose evidence is not represented by an event are derived from
        the corresponding durable row state.
        """
        def from_rows(
            rows: list[tuple[str, int]],
            *,
            pending: int,
            replayed: int,
            probes: int,
            quarantined: int,
            timing_rows: list[dict[str, Any]],
            wait_rows: list[dict[str, Any]],
        ) -> dict[str, int | float]:
            values = {key: int(value) for key, value in rows}
            runs: dict[str, dict[str, float]] = {}
            queue_waits: list[float] = []
            for row in wait_rows:
                created = row.get("item_created")
                claimed = row.get("attempt_created")
                if created is not None and claimed is not None:
                    queue_waits.append(max(0.0, float(claimed) - float(created)))
            for row in timing_rows:
                run_id = str(row["goal_run_id"])
                run = runs.setdefault(run_id, {"start": float(row["item_created"]), "end": 0.0, "busy": 0.0, "max_parallel": 1.0})
                run["start"] = min(run["start"], float(row["item_created"]))
                item_end = float(row.get("item_updated") or 0.0)
                release = row.get("released_at")
                acquired = row.get("acquired_at")
                run["end"] = max(run["end"], item_end, float(release or 0.0), float(acquired or 0.0))
                if acquired is not None:
                    run["busy"] += max(0.0, float(release or time.time()) - float(acquired))
                budget = _decode(row.get("budget_json"), {})
                run["max_parallel"] = max(1.0, float(budget.get("max_parallel", 4)))
            capacity = 0.0
            busy = 0.0
            current = time.time()
            for run in runs.values():
                end = max(run["end"], current if run["busy"] else run["start"])
                capacity += max(0.0, end - run["start"]) * run["max_parallel"]
                busy += run["busy"]
            slot_utilization = min(1.0, busy / capacity) if capacity > 0 else 0.0
            return {
                "jobs_enqueued": values.get("work_item.created", 0),
                "jobs_claimed": values.get("work_item.claimed", 0),
                "lease_expired": values.get("lease.expired", 0),
                "lease_reclaimed": values.get("recovery.decision.RETRY_SAFE", 0) + values.get("recovery.decision.IDEMPOTENT_RETRY", 0),
                "fencing_rejected": values.get("work_item.fenced_out", 0),
                "duplicate_completion_suppressed": values.get("work_item.completion_deduplicated", 0),
                "dangling_detected": values.get("recovery.decision.total", 0),
                "dangling_recovered": values.get("recovery.decision.COMPLETED_FROM_EVIDENCE", 0) + values.get("recovery.decision.RETRY_SAFE", 0) + values.get("recovery.decision.IDEMPOTENT_RETRY", 0),
                "side_effect_probe": probes,
                "side_effect_quarantined": quarantined,
                "outbox_pending": pending,
                "outbox_replayed": replayed,
                "finalization_resumed": values.get("finalization.resumed", 0),
                "partial_result_preserved": values.get("work_item.unknown", 0) + values.get("finalization.partial_completed", 0),
                "scheduler_slot_utilization": round(slot_utilization, 4),
                "queue_wait_ms": round((sum(queue_waits) / len(queue_waits)) * 1000) if queue_waits else 0,
            }

        if self.backend == "sqlite":
            def read_metrics(conn: Any) -> dict[str, int | float]:
                rows = conn.execute("SELECT event_type,COUNT(*) AS count FROM execution_events GROUP BY event_type").fetchall()
                decisions = conn.execute("SELECT decision,COUNT(*) AS count FROM recovery_decisions GROUP BY decision").fetchall()
                event_rows = [(str(row[0]), int(row[1])) for row in rows]
                event_rows.extend((f"recovery.decision.{row[0]}", int(row[1])) for row in decisions)
                event_rows.append(("recovery.decision.total", sum(int(row[1]) for row in decisions)))
                pending = int(conn.execute("SELECT COUNT(*) FROM execution_outbox WHERE published_at IS NULL").fetchone()[0])
                replayed = int(conn.execute("SELECT COALESCE(SUM(CASE WHEN publish_attempts > 1 THEN publish_attempts - 1 ELSE 0 END),0) FROM execution_outbox").fetchone()[0])
                probes = int(conn.execute("SELECT COUNT(*) FROM side_effects WHERE probe_result_json IS NOT NULL").fetchone()[0])
                quarantined = int(conn.execute("SELECT COUNT(*) FROM work_items WHERE state='quarantined_unknown'").fetchone()[0])
                timing_rows = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT wi.goal_run_id,wi.created_at AS item_created,wi.updated_at AS item_updated,"
                        "el.acquired_at,el.released_at,gr.budget_json "
                        "FROM work_items wi JOIN goal_runs gr ON gr.id=wi.goal_run_id "
                        "LEFT JOIN execution_leases el ON el.work_item_id=wi.id"
                    ).fetchall()
                ]
                wait_rows = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT wi.created_at AS item_created,wa.created_at AS attempt_created "
                        "FROM work_attempts wa JOIN work_items wi ON wi.id=wa.work_item_id"
                    ).fetchall()
                ]
                return from_rows(
                    event_rows,
                    pending=pending,
                    replayed=replayed,
                    probes=probes,
                    quarantined=quarantined,
                    timing_rows=timing_rows,
                    wait_rows=wait_rows,
                )

            return await asyncio.to_thread(lambda: self._sqlite_read(read_metrics))

        async def read_metrics_pg(conn: Any) -> dict[str, int | float]:
            rows = await conn.fetch("SELECT event_type,COUNT(*) AS count FROM execution_events GROUP BY event_type")
            decisions = await conn.fetch("SELECT decision,COUNT(*) AS count FROM recovery_decisions GROUP BY decision")
            event_rows = [(str(row["event_type"]), int(row["count"])) for row in rows]
            event_rows.extend((f"recovery.decision.{row['decision']}", int(row["count"])) for row in decisions)
            event_rows.append(("recovery.decision.total", sum(int(row["count"]) for row in decisions)))
            pending = int(await conn.fetchval("SELECT COUNT(*) FROM execution_outbox WHERE published_at IS NULL"))
            replayed = int(await conn.fetchval("SELECT COALESCE(SUM(CASE WHEN publish_attempts > 1 THEN publish_attempts - 1 ELSE 0 END),0) FROM execution_outbox"))
            probes = int(await conn.fetchval("SELECT COUNT(*) FROM side_effects WHERE probe_result_json IS NOT NULL"))
            quarantined = int(await conn.fetchval("SELECT COUNT(*) FROM work_items WHERE state='quarantined_unknown'"))
            timing_rows = [
                dict(row)
                for row in await conn.fetch(
                    "SELECT wi.goal_run_id,wi.created_at AS item_created,wi.updated_at AS item_updated,"
                    "el.acquired_at,el.released_at,gr.budget_json "
                    "FROM work_items wi JOIN goal_runs gr ON gr.id=wi.goal_run_id "
                    "LEFT JOIN execution_leases el ON el.work_item_id=wi.id"
                )
            ]
            wait_rows = [
                dict(row)
                for row in await conn.fetch(
                    "SELECT wi.created_at AS item_created,wa.created_at AS attempt_created "
                    "FROM work_attempts wa JOIN work_items wi ON wi.id=wa.work_item_id"
                )
            ]
            return from_rows(
                event_rows,
                pending=pending,
                replayed=replayed,
                probes=probes,
                quarantined=quarantined,
                timing_rows=timing_rows,
                wait_rows=wait_rows,
            )

        return await self._pg_tx(read_metrics_pg)
