"""Durable Memory/Skill/Continuity/Learning projections.

The store uses the same ``VEYA_EXECUTION_DATABASE_URL`` authority as GoalRun.
It supports SQLite only for isolated tests/development and refuses a SQLite
production authority.  All mutating operations are transactional and emit a
durable event plus an outbox row in the same transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from runtime.execution.durable import DurableExecutionError
from runtime.execution.schema import POSTGRES_SCHEMA, SCHEMA_VERSION, SQLITE_SCHEMA

_VALID_SCOPE_TYPES = frozenset({"user", "workspace", "session"})
_VALID_MEMORY_TYPES = frozenset({"episodic", "semantic", "procedural", "preference", "decision"})
_VALID_MEMORY_STATES = frozenset({"candidate", "active", "superseded", "invalidated", "forgotten"})
_VALID_SKILL_STATES = frozenset({"candidate", "active", "deprecated", "blocked"})
_VALID_TRUST = frozenset({"trusted", "review_required", "blocked"})

_FEATURE_FLAGS = (
    "VEYA_MEMORY_V2",
    "VEYA_MEMORY_CANDIDATES",
    "VEYA_MEMORY_CONFLICT_DETECTION",
    "VEYA_SKILL_V2",
    "VEYA_SKILL_TEACHING",
    "VEYA_SKILL_VERSIONING",
    "VEYA_CONTINUITY_V1",
    "VEYA_PERSONAL_CONTEXT_UI",
    "VEYA_LONG_TERM_LEARNING",
    "VEYA_PERSONAL_AGENT_EVAL",
)


def _flag(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() not in {"", "0", "false", "off", "no"}


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default


_GOLD_METRIC_NAMES = (
    "retrieval_precision",
    "memory_precision",
    "memory_recall_when_needed",
    "unnecessary_memory_use_rate",
    "stale_memory_use_rate",
    "memory_conflict_resolution_accuracy",
    "memory_correction_success_rate",
    "skill_activation_precision",
    "wrong_skill_activation_rate",
    "skill_reuse_success_rate",
    "skill_regression_rate",
    "skill_version_selection_accuracy",
    "continuity_task_recovery_accuracy",
    "continuity_state_restore_accuracy",
    "learning_candidate_precision",
    "learning_regression_escape_rate",
)


def _load_gold_benchmark() -> dict[str, Any] | None:
    """Load approved benchmark evidence without making it runtime state."""
    configured = os.environ.get("VEYA_PERSONAL_AGENT_GOLD_REPORT")
    candidates = [Path(configured)] if configured else []
    candidates.extend(
        [
            Path(__file__).resolve().parents[2]
            / "evals"
            / "personal_agent_gold"
            / "results"
            / "latest.json",
            Path.cwd() / "evals" / "personal_agent_gold" / "results" / "latest.json",
        ]
    )
    for path in candidates:
        if not path or not path.is_file():
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            report.get("dataset_version") != "personal-agent-gold-v1"
            or report.get("runtime_schema_version") != SCHEMA_VERSION
            or report.get("approved_count", 0) != report.get("scenario_count", -1)
            or report.get("approved_count", 0) <= 0
        ):
            continue
        metrics = {
            name: report["metrics"][name]
            for name in _GOLD_METRIC_NAMES
            if isinstance(report.get("metrics", {}).get(name), dict)
            and report["metrics"][name].get("rate") is not None
        }
        if len(metrics) != len(_GOLD_METRIC_NAMES):
            continue
        return {
            "dataset_version": report["dataset_version"],
            "eval_run_id": report.get("eval_run_id"),
            "git_sha": report.get("git_sha"),
            "approved_count": report["approved_count"],
            "status": report.get("status"),
            "metrics": metrics,
            "source": "approved_personal_agent_gold",
        }
    return None


def _attach_gold_benchmark(values: dict[str, Any]) -> dict[str, Any]:
    report = _load_gold_benchmark()
    values["gold_benchmark"] = report
    if report is None:
        return values
    for name, metric in report["metrics"].items():
        values[name] = metric["rate"]
    values["unnecessary_memory_use"] = values["unnecessary_memory_use_rate"]
    return values


def _rowdict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def canonical_fingerprint(content: str) -> str:
    """Stable exact fingerprint used before any optional semantic index."""
    normalized = re.sub(r"\s+", " ", re.sub(r"[^\w\u4e00-\u9fff]+", " ", content.lower())).strip()
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _tokens(content: str) -> set[str]:
    return {token for token in re.findall(r"[\w\u4e00-\u9fff]+", content.lower()) if len(token) > 1}


def _similarity(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 1.0 if left.strip().lower() == right.strip().lower() else 0.0
    return len(a & b) / len(a | b)


def _near_duplicate(left: str, right: str) -> bool:
    if canonical_fingerprint(left) == canonical_fingerprint(right):
        return True
    a, b = _tokens(left), _tokens(right)
    return bool(a and b and (a <= b or b <= a or _similarity(left, right) >= 0.82))


def _conflict(left: str, right: str) -> bool:
    """Conservative conflict signal; it never selects a winner by itself."""
    if _near_duplicate(left, right):
        return False
    a, b = _tokens(left), _tokens(right)
    shared = a & b
    if len(shared) < 2:
        return False
    # Common instruction anchors make differing tails a useful review signal.
    anchors = {
        "use",
        "using",
        "prefer",
        "always",
        "never",
        "统一",
        "改成",
        "项目",
        "测试",
        "review",
    }
    return bool(shared & anchors) or _similarity(left, right) >= 0.35


class PersonalRuntimeError(DurableExecutionError):
    """Stable errors for personal runtime APIs."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code, f"{code}: {message}" if message else code)


class PersonalRuntimeStore:
    """One durable repository for Personal Agent Runtime state."""

    def __init__(
        self,
        *,
        dsn: str | None = None,
        sqlite_path: str | Path | None = None,
        production: bool | None = None,
    ) -> None:
        self.dsn = (
            dsn
            if dsn is not None
            else None
            if sqlite_path is not None
            else (os.environ.get("VEYA_EXECUTION_DATABASE_URL") or os.environ.get("DATABASE_URL"))
        )
        self.backend = (
            "postgres"
            if (self.dsn or "").startswith(("postgres://", "postgresql://"))
            else "sqlite"
        )
        production_mode = (
            os.environ.get("VEYA_EXECUTION_PRODUCTION", "0").strip().lower()
            not in {"", "0", "false", "off", "no"}
            if production is None and sqlite_path is None
            else bool(production)
        )
        if self.backend == "sqlite" and production_mode:
            raise PersonalRuntimeError(
                "CONFIG_INVALID", "SQLite is not approved for production personal runtime authority"
            )
        self.sqlite_path = Path(
            sqlite_path
            or os.environ.get("VEYA_EXECUTION_SQLITE_PATH", ".veya/execution-runtime.sqlite3")
        ).expanduser()
        self.production = production_mode
        self._pool: Any = None
        self._sqlite_memory: sqlite3.Connection | None = None
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None

    async def connect(self) -> None:
        if self.backend == "postgres":
            try:
                import asyncpg
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise PersonalRuntimeError(
                    "CONFIG_INVALID", "asyncpg is required for PostgreSQL personal runtime"
                ) from exc
            self._pool = await asyncpg.create_pool(
                self.dsn, min_size=1, max_size=5, command_timeout=10
            )
        else:
            await asyncio.to_thread(self._sqlite_prepare)
        await self.migrate()
        self._started = True
        self._loop = asyncio.get_running_loop()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        self._started = False
        self._loop = None

    def run_sync(self, coroutine: Awaitable[Any]) -> Any:
        """Bridge a synchronous legacy callback onto the owning event loop."""
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if self._loop is not None and self._loop.is_running() and self._loop is not current:
            return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()
        return asyncio.run(coroutine)

    async def migrate(self) -> None:
        if self.backend == "postgres":
            if self._pool is None:
                raise PersonalRuntimeError(
                    "DATABASE_UNAVAILABLE", "personal runtime is not connected"
                )
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))", "veya:execution:schema"
                )
                for statement in POSTGRES_SCHEMA:
                    await conn.execute(statement)
                await conn.execute(
                    "INSERT INTO execution_schema_meta(version, applied_at) VALUES($1,$2) ON CONFLICT(version) DO NOTHING",
                    SCHEMA_VERSION,
                    _now(),
                )
        else:
            await asyncio.to_thread(self._sqlite_migrate)

    def _sqlite_prepare(self) -> None:
        if str(self.sqlite_path) == ":memory:":
            if self._sqlite_memory is None:
                self._sqlite_memory = sqlite3.connect(":memory:", check_same_thread=False)
                self._sqlite_memory.row_factory = sqlite3.Row
                self._sqlite_memory.execute("PRAGMA foreign_keys=ON")
        else:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    def _sqlite_connection(self) -> sqlite3.Connection:
        self._sqlite_prepare()
        if self._sqlite_memory is not None and str(self.sqlite_path) == ":memory:":
            return self._sqlite_memory
        conn = sqlite3.connect(str(self.sqlite_path), timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _sqlite_migrate(self) -> None:
        conn = self._sqlite_connection()
        try:
            for statement in SQLITE_SCHEMA:
                conn.execute(statement)
            conn.execute(
                "INSERT OR IGNORE INTO execution_schema_meta(version, applied_at) VALUES(?,?)",
                (SCHEMA_VERSION, _now()),
            )
            conn.commit()
        finally:
            if conn is not self._sqlite_memory:
                conn.close()

    def _sqlite_tx(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
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
            raise PersonalRuntimeError("DATABASE_UNAVAILABLE", "personal runtime is not connected")
        async with self._pool.acquire() as conn, conn.transaction():
            return await fn(conn)

    async def _ensure_connected(self) -> None:
        if not self._started:
            await self.connect()

    # ── durable personal events/outbox ──────────────────────────────
    def _event_sqlite(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        payload: dict[str, Any],
        *,
        trace_id: str | None,
        session_id: str | None,
        task_id: str | None,
        workspace_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        event_id = _new_id("pevent")
        now = _now()
        conn.execute(
            "INSERT OR IGNORE INTO personal_events(id,event_type,trace_id,session_id,task_id,workspace_id,payload_json,schema_version,occurred_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                event_type,
                trace_id,
                session_id,
                task_id,
                workspace_id,
                _json(payload),
                1,
                now,
                idempotency_key,
            ),
        )
        row = conn.execute(
            "SELECT * FROM personal_events WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        assert row is not None
        data = dict(row)
        conn.execute(
            "INSERT OR IGNORE INTO personal_outbox(event_id,next_attempt_at) VALUES(?,?)",
            (data["id"], now),
        )
        data["payload"] = _loads(data.pop("payload_json"), {})
        return data

    async def _event_pg(
        self,
        conn: Any,
        event_type: str,
        payload: dict[str, Any],
        *,
        trace_id: str | None,
        session_id: str | None,
        task_id: str | None,
        workspace_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        event_id = _new_id("pevent")
        now = _now()
        await conn.execute(
            "INSERT INTO personal_events(id,event_type,trace_id,session_id,task_id,workspace_id,payload_json,schema_version,occurred_at,idempotency_key) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT(idempotency_key) DO NOTHING",
            event_id,
            event_type,
            trace_id,
            session_id,
            task_id,
            workspace_id,
            _json(payload),
            1,
            now,
            idempotency_key,
        )
        row = await conn.fetchrow(
            "SELECT * FROM personal_events WHERE idempotency_key=$1", idempotency_key
        )
        if row is None:
            raise PersonalRuntimeError("EVENT_FAILED", "personal event was not visible")
        data = dict(row)
        await conn.execute(
            "INSERT INTO personal_outbox(event_id,next_attempt_at) VALUES($1,$2) ON CONFLICT(event_id) DO NOTHING",
            data["id"],
            now,
        )
        data["payload"] = _loads(data.pop("payload_json"), {})
        return data

    async def record_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        workspace_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_connected()
        key = (
            idempotency_key
            or f"personal-event:{event_type}:{hashlib.sha256(_json(payload).encode()).hexdigest()}"
        )
        if self.backend == "sqlite":
            return await asyncio.to_thread(
                self._sqlite_tx,
                lambda c: self._event_sqlite(
                    c,
                    event_type,
                    payload,
                    trace_id=trace_id,
                    session_id=session_id,
                    task_id=task_id,
                    workspace_id=workspace_id,
                    idempotency_key=key,
                ),
            )
        return await self._pg_tx(
            lambda c: self._event_pg(
                c,
                event_type,
                payload,
                trace_id=trace_id,
                session_id=session_id,
                task_id=task_id,
                workspace_id=workspace_id,
                idempotency_key=key,
            )
        )

    # ── memory ──────────────────────────────────────────────────────
    @staticmethod
    def _validate_memory(scope_type: str, memory_type: str, content: str) -> None:
        if scope_type not in _VALID_SCOPE_TYPES:
            raise PersonalRuntimeError("INVALID_SCOPE", scope_type)
        if memory_type not in _VALID_MEMORY_TYPES:
            raise PersonalRuntimeError("INVALID_MEMORY_TYPE", memory_type)
        if not str(content).strip():
            raise PersonalRuntimeError("INVALID_CONTENT", "memory content is empty")

    @staticmethod
    def _memory_out(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for key in (
            "source_event_ids",
            "source_session_ids",
            "source_task_ids",
            "supersedes",
            "superseded_by",
            "tags",
            "provenance_json",
        ):
            raw = out.pop(key, "[]")
            out[key.removesuffix("_json")] = _loads(raw, {} if key == "provenance_json" else [])
        return out

    @staticmethod
    def _candidate_out(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for key in ("source_event_ids", "source_session_ids", "source_task_ids", "conflicts_with"):
            out[key] = _loads(out.get(key), [])
        return out

    def _memory_conflicts_sqlite(
        self,
        conn: sqlite3.Connection,
        scope_type: str,
        scope_id: str,
        memory_type: str,
        content: str,
    ) -> list[str]:
        rows = conn.execute(
            "SELECT id,content FROM memory_records WHERE scope_type=? AND scope_id=? AND memory_type=? AND status='active'",
            (scope_type, scope_id, memory_type),
        ).fetchall()
        return [str(row[0]) for row in rows if _conflict(content, str(row[1]))]

    async def _memory_conflicts_pg(
        self, conn: Any, scope_type: str, scope_id: str, memory_type: str, content: str
    ) -> list[str]:
        rows = await conn.fetch(
            "SELECT id,content FROM memory_records WHERE scope_type=$1 AND scope_id=$2 AND memory_type=$3 AND status='active'",
            scope_type,
            scope_id,
            memory_type,
        )
        return [str(row["id"]) for row in rows if _conflict(content, str(row["content"]))]

    async def create_memory_candidate(
        self,
        content: str,
        *,
        scope_type: str,
        scope_id: str,
        memory_type: str = "semantic",
        source_event_ids: Iterable[str] = (),
        source_session_ids: Iterable[str] = (),
        source_task_ids: Iterable[str] = (),
        confidence: float = 0.5,
        reason: str = "",
        provenance: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        self._validate_memory(scope_type, memory_type, content)
        if (
            not reason
            and not provenance
            and not any((source_event_ids, source_session_ids, source_task_ids))
        ):
            raise PersonalRuntimeError(
                "PROVENANCE_REQUIRED", "memory candidates require provenance"
            )
        confidence = max(0.0, min(1.0, float(confidence)))
        event_ids, session_ids, task_ids = (
            list(source_event_ids),
            list(source_session_ids),
            list(source_task_ids),
        )
        candidate_id = _new_id("memory_candidate")
        fingerprint = canonical_fingerprint(content)
        now = _now()
        prov = dict(provenance or {})
        prov.setdefault("reason", reason)

        def insert_sqlite(conn: sqlite3.Connection) -> dict[str, Any]:
            conflicts = self._memory_conflicts_sqlite(
                conn, scope_type, scope_id, memory_type, content
            )
            conn.execute(
                "INSERT INTO memory_candidates(id,proposed_content,scope_type,scope_id,memory_type,source_event_ids,source_session_ids,source_task_ids,confidence,reason,conflicts_with,canonical_fingerprint,status,created_at,updated_at,schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    candidate_id,
                    content,
                    scope_type,
                    scope_id,
                    memory_type,
                    _json(event_ids),
                    _json(session_ids),
                    _json(task_ids),
                    confidence,
                    reason,
                    _json(conflicts),
                    fingerprint,
                    "candidate",
                    now,
                    now,
                    2,
                ),
            )
            event = self._event_sqlite(
                conn,
                "memory.candidate_created",
                {
                    "candidate_id": candidate_id,
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "memory_type": memory_type,
                    "confidence": confidence,
                    "conflicts_with": conflicts,
                    "provenance": prov,
                },
                trace_id=trace_id,
                session_id=session_ids[0] if session_ids else None,
                task_id=task_ids[0] if task_ids else None,
                workspace_id=scope_id if scope_type == "workspace" else None,
                idempotency_key=f"memory-candidate:{candidate_id}",
            )
            if conflicts:
                self._event_sqlite(
                    conn,
                    "memory.conflict_detected",
                    {"candidate_id": candidate_id, "conflicts_with": conflicts},
                    trace_id=trace_id,
                    session_id=session_ids[0] if session_ids else None,
                    task_id=task_ids[0] if task_ids else None,
                    workspace_id=scope_id if scope_type == "workspace" else None,
                    idempotency_key=f"memory-conflict:{candidate_id}",
                )
            if not event_ids:
                event_ids.append(str(event["id"]))
                conn.execute(
                    "UPDATE memory_candidates SET source_event_ids=?,updated_at=? WHERE id=?",
                    (_json(event_ids), _now(), candidate_id),
                )
            row = conn.execute(
                "SELECT * FROM memory_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            assert row is not None
            return self._candidate_out(dict(row))

        async def insert_pg(conn: Any) -> dict[str, Any]:
            conflicts = await self._memory_conflicts_pg(
                conn, scope_type, scope_id, memory_type, content
            )
            await conn.execute(
                "INSERT INTO memory_candidates(id,proposed_content,scope_type,scope_id,memory_type,source_event_ids,source_session_ids,source_task_ids,confidence,reason,conflicts_with,canonical_fingerprint,status,created_at,updated_at,schema_version) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)",
                candidate_id,
                content,
                scope_type,
                scope_id,
                memory_type,
                _json(event_ids),
                _json(session_ids),
                _json(task_ids),
                confidence,
                reason,
                _json(conflicts),
                fingerprint,
                "candidate",
                now,
                now,
                2,
            )
            event = await self._event_pg(
                conn,
                "memory.candidate_created",
                {
                    "candidate_id": candidate_id,
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "memory_type": memory_type,
                    "confidence": confidence,
                    "conflicts_with": conflicts,
                    "provenance": prov,
                },
                trace_id=trace_id,
                session_id=session_ids[0] if session_ids else None,
                task_id=task_ids[0] if task_ids else None,
                workspace_id=scope_id if scope_type == "workspace" else None,
                idempotency_key=f"memory-candidate:{candidate_id}",
            )
            if conflicts:
                await self._event_pg(
                    conn,
                    "memory.conflict_detected",
                    {"candidate_id": candidate_id, "conflicts_with": conflicts},
                    trace_id=trace_id,
                    session_id=session_ids[0] if session_ids else None,
                    task_id=task_ids[0] if task_ids else None,
                    workspace_id=scope_id if scope_type == "workspace" else None,
                    idempotency_key=f"memory-conflict:{candidate_id}",
                )
            if not event_ids:
                event_ids.append(str(event["id"]))
                await conn.execute(
                    "UPDATE memory_candidates SET source_event_ids=$1,updated_at=$2 WHERE id=$3",
                    _json(event_ids),
                    _now(),
                    candidate_id,
                )
            row = await conn.fetchrow("SELECT * FROM memory_candidates WHERE id=$1", candidate_id)
            if row is None:
                raise PersonalRuntimeError("MEMORY_FAILED", "candidate was not visible")
            return self._candidate_out(dict(row))

        await self._ensure_connected()
        if self.backend == "sqlite":
            return await asyncio.to_thread(self._sqlite_tx, insert_sqlite)
        return await self._pg_tx(insert_pg)

    async def _commit_candidate_tx(
        self,
        conn: Any,
        candidate: dict[str, Any],
        *,
        allow_conflicts: bool,
        supersedes: list[str],
        trace_id: str | None,
        sqlite: bool,
    ) -> dict[str, Any]:
        candidate_id = str(candidate["id"])
        if candidate.get("status") != "candidate":
            raise PersonalRuntimeError(
                "INVALID_STATE", f"candidate {candidate_id} is {candidate.get('status')}"
            )
        conflicts = list(candidate.get("conflicts_with") or [])
        if conflicts and not allow_conflicts and not supersedes:
            raise PersonalRuntimeError("CONFLICT_REVIEW_REQUIRED", ",".join(conflicts))
        scope_type, scope_id, memory_type = (
            candidate["scope_type"],
            candidate["scope_id"],
            candidate["memory_type"],
        )
        content = candidate["proposed_content"]
        # Exact/near duplicates return the existing active fact and never create
        # a parallel fact.  The candidate remains auditable as committed_dedup.
        if sqlite:
            rows = conn.execute(
                "SELECT * FROM memory_records WHERE scope_type=? AND scope_id=? AND memory_type=? AND status='active'",
                (scope_type, scope_id, memory_type),
            ).fetchall()
        else:
            rows = await conn.fetch(
                "SELECT * FROM memory_records WHERE scope_type=$1 AND scope_id=$2 AND memory_type=$3 AND status='active'",
                scope_type,
                scope_id,
                memory_type,
            )
        for raw in rows:
            row = dict(raw)
            if _near_duplicate(content, str(row["content"])) and not supersedes:
                merged_event_ids = list(
                    dict.fromkeys(
                        [
                            *_loads(row.get("source_event_ids"), []),
                            *_loads(candidate.get("source_event_ids"), []),
                        ]
                    )
                )
                merged_session_ids = list(
                    dict.fromkeys(
                        [
                            *_loads(row.get("source_session_ids"), []),
                            *_loads(candidate.get("source_session_ids"), []),
                        ]
                    )
                )
                merged_task_ids = list(
                    dict.fromkeys(
                        [
                            *_loads(row.get("source_task_ids"), []),
                            *_loads(candidate.get("source_task_ids"), []),
                        ]
                    )
                )
                if sqlite:
                    conn.execute(
                        "UPDATE memory_records SET source_event_ids=?,source_session_ids=?,source_task_ids=?,last_verified_at=?,updated_at=? WHERE id=?",
                        (
                            _json(merged_event_ids),
                            _json(merged_session_ids),
                            _json(merged_task_ids),
                            _now(),
                            _now(),
                            row["id"],
                        ),
                    )
                    conn.execute(
                        "UPDATE memory_candidates SET status='committed_dedup',updated_at=? WHERE id=?",
                        (_now(), candidate_id),
                    )
                else:
                    await conn.execute(
                        "UPDATE memory_records SET source_event_ids=$1,source_session_ids=$2,source_task_ids=$3,last_verified_at=$4,updated_at=$4 WHERE id=$5",
                        _json(merged_event_ids),
                        _json(merged_session_ids),
                        _json(merged_task_ids),
                        _now(),
                        row["id"],
                    )
                    await conn.execute(
                        "UPDATE memory_candidates SET status='committed_dedup',updated_at=$1 WHERE id=$2",
                        _now(),
                        candidate_id,
                    )
                if sqlite:
                    row = dict(conn.execute("SELECT * FROM memory_records WHERE id=?", (row["id"],)).fetchone())
                else:
                    row = dict(await conn.fetchrow("SELECT * FROM memory_records WHERE id=$1", row["id"]))
                return {
                    "status": "deduplicated",
                    "candidate_id": candidate_id,
                    "record": self._memory_out(row),
                }
        record_id = _new_id("memory")
        now = _now()
        supersede_ids = list(dict.fromkeys([*supersedes, *(conflicts if allow_conflicts else [])]))
        provenance = {"candidate_id": candidate_id, "reason": candidate.get("reason", "")}
        if sqlite:
            conn.execute(
                "INSERT INTO memory_records(id,scope_type,scope_id,memory_type,content,source_event_ids,source_session_ids,source_task_ids,provenance_json,confidence,created_at,updated_at,last_verified_at,status,supersedes,superseded_by,tags,canonical_fingerprint,schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id,
                    scope_type,
                    scope_id,
                    memory_type,
                    content,
                    candidate["source_event_ids"],
                    candidate["source_session_ids"],
                    candidate["source_task_ids"],
                    _json(provenance),
                    candidate["confidence"],
                    now,
                    now,
                    now,
                    "active",
                    _json(supersede_ids),
                    "[]",
                    "[]",
                    candidate["canonical_fingerprint"],
                    2,
                ),
            )
            conn.execute(
                "UPDATE memory_candidates SET status='committed',updated_at=? WHERE id=?",
                (now, candidate_id),
            )
            for old_id in supersede_ids:
                conn.execute(
                    "UPDATE memory_records SET status='superseded',superseded_by=?,updated_at=? WHERE id=? AND status='active'",
                    (_json([record_id]), now, old_id),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO memory_edges(id,source_id,target_id,edge_type,created_at) VALUES(?,?,?,?,?)",
                    (_new_id("medge"), old_id, record_id, "supersedes", now),
                )
            event = self._event_sqlite(
                conn,
                "memory.committed",
                {"memory_id": record_id, "candidate_id": candidate_id, "supersedes": supersede_ids},
                trace_id=trace_id,
                session_id=(_loads(candidate["source_session_ids"], []) or [None])[0],
                task_id=(_loads(candidate["source_task_ids"], []) or [None])[0],
                workspace_id=scope_id if scope_type == "workspace" else None,
                idempotency_key=f"memory-committed:{candidate_id}",
            )
            if _loads(candidate["source_event_ids"], []) == []:
                conn.execute(
                    "UPDATE memory_records SET source_event_ids=? WHERE id=?",
                    (_json([event["id"]]), record_id),
                )
            row = conn.execute("SELECT * FROM memory_records WHERE id=?", (record_id,)).fetchone()
            assert row is not None
            return {
                "status": "committed",
                "candidate_id": candidate_id,
                "record": self._memory_out(dict(row)),
            }
        await conn.execute(
            "INSERT INTO memory_records(id,scope_type,scope_id,memory_type,content,source_event_ids,source_session_ids,source_task_ids,provenance_json,confidence,created_at,updated_at,last_verified_at,status,supersedes,superseded_by,tags,canonical_fingerprint,schema_version) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)",
            record_id,
            scope_type,
            scope_id,
            memory_type,
            content,
            _json(candidate["source_event_ids"]),
            _json(candidate["source_session_ids"]),
            _json(candidate["source_task_ids"]),
            _json(provenance),
            candidate["confidence"],
            now,
            now,
            now,
            "active",
            _json(supersede_ids),
            "[]",
            "[]",
            candidate["canonical_fingerprint"],
            2,
        )
        await conn.execute(
            "UPDATE memory_candidates SET status='committed',updated_at=$1 WHERE id=$2",
            now,
            candidate_id,
        )
        for old_id in supersede_ids:
            await conn.execute(
                "UPDATE memory_records SET status='superseded',superseded_by=$1,updated_at=$2 WHERE id=$3 AND status='active'",
                _json([record_id]),
                now,
                old_id,
            )
            await conn.execute(
                "INSERT INTO memory_edges(id,source_id,target_id,edge_type,created_at) VALUES($1,$2,$3,$4,$5) ON CONFLICT(source_id,target_id,edge_type) DO NOTHING",
                _new_id("medge"),
                old_id,
                record_id,
                "supersedes",
                now,
            )
        event = await self._event_pg(
            conn,
            "memory.committed",
            {"memory_id": record_id, "candidate_id": candidate_id, "supersedes": supersede_ids},
            trace_id=trace_id,
            session_id=(_loads(candidate["source_session_ids"], []) or [None])[0],
            task_id=(_loads(candidate["source_task_ids"], []) or [None])[0],
            workspace_id=scope_id if scope_type == "workspace" else None,
            idempotency_key=f"memory-committed:{candidate_id}",
        )
        if _loads(candidate["source_event_ids"], []) == []:
            await conn.execute(
                "UPDATE memory_records SET source_event_ids=$1 WHERE id=$2",
                _json([event["id"]]),
                record_id,
            )
        row = await conn.fetchrow("SELECT * FROM memory_records WHERE id=$1", record_id)
        if row is None:
            raise PersonalRuntimeError("MEMORY_FAILED", "record was not visible")
        return {
            "status": "committed",
            "candidate_id": candidate_id,
            "record": self._memory_out(dict(row)),
        }

    async def commit_memory_candidate(
        self,
        candidate_id: str,
        *,
        allow_conflicts: bool = False,
        supersedes: Iterable[str] = (),
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_connected()
        ids = list(supersedes)
        if self.backend == "sqlite":
            return await asyncio.to_thread(
                self._sqlite_commit_candidate, candidate_id, allow_conflicts, ids, trace_id
            )

        async def tx(conn: Any) -> dict[str, Any]:
            row = await conn.fetchrow(
                "SELECT * FROM memory_candidates WHERE id=$1 FOR UPDATE", candidate_id
            )
            if row is None:
                raise PersonalRuntimeError("NOT_FOUND", candidate_id)
            return await self._commit_candidate_tx(
                conn,
                self._candidate_out(dict(row)),
                allow_conflicts=allow_conflicts,
                supersedes=ids,
                trace_id=trace_id,
                sqlite=False,
            )

        return await self._pg_tx(tx)

    def _sqlite_commit_candidate(
        self, candidate_id: str, allow_conflicts: bool, ids: list[str], trace_id: str | None
    ) -> dict[str, Any]:
        conn = self._sqlite_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM memory_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise PersonalRuntimeError("NOT_FOUND", candidate_id)
            candidate = self._candidate_out(dict(row))
            if candidate.get("status") != "candidate":
                raise PersonalRuntimeError("INVALID_STATE", candidate_id)
            conflicts = list(candidate.get("conflicts_with") or [])
            if conflicts and not allow_conflicts and not ids:
                raise PersonalRuntimeError("CONFLICT_REVIEW_REQUIRED", ",".join(conflicts))
            scope_type, scope_id, memory_type, content = (
                candidate["scope_type"],
                candidate["scope_id"],
                candidate["memory_type"],
                candidate["proposed_content"],
            )
            rows = conn.execute(
                "SELECT * FROM memory_records WHERE scope_type=? AND scope_id=? AND memory_type=? AND status='active'",
                (scope_type, scope_id, memory_type),
            ).fetchall()
            for raw in rows:
                record = dict(raw)
                if _near_duplicate(content, str(record["content"])) and not ids:
                    merged_event_ids = list(
                        dict.fromkeys(
                            [
                                *_loads(record.get("source_event_ids"), []),
                                *_loads(candidate.get("source_event_ids"), []),
                            ]
                        )
                    )
                    merged_session_ids = list(
                        dict.fromkeys(
                            [
                                *_loads(record.get("source_session_ids"), []),
                                *_loads(candidate.get("source_session_ids"), []),
                            ]
                        )
                    )
                    merged_task_ids = list(
                        dict.fromkeys(
                            [
                                *_loads(record.get("source_task_ids"), []),
                                *_loads(candidate.get("source_task_ids"), []),
                            ]
                        )
                    )
                    conn.execute(
                        "UPDATE memory_records SET source_event_ids=?,source_session_ids=?,source_task_ids=?,last_verified_at=?,updated_at=? WHERE id=?",
                        (
                            _json(merged_event_ids),
                            _json(merged_session_ids),
                            _json(merged_task_ids),
                            _now(),
                            _now(),
                            record["id"],
                        ),
                    )
                    conn.execute(
                        "UPDATE memory_candidates SET status='committed_dedup',updated_at=? WHERE id=?",
                        (_now(), candidate_id),
                    )
                    record = dict(
                        conn.execute(
                            "SELECT * FROM memory_records WHERE id=?", (record["id"],)
                        ).fetchone()
                    )
                    conn.commit()
                    return {
                        "status": "deduplicated",
                        "candidate_id": candidate_id,
                        "record": self._memory_out(record),
                    }
            record_id = _new_id("memory")
            now = _now()
            supersede_ids = list(dict.fromkeys([*ids, *(conflicts if allow_conflicts else [])]))
            conn.execute(
                "INSERT INTO memory_records(id,scope_type,scope_id,memory_type,content,source_event_ids,source_session_ids,source_task_ids,provenance_json,confidence,created_at,updated_at,last_verified_at,status,supersedes,superseded_by,tags,canonical_fingerprint,schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id,
                    scope_type,
                    scope_id,
                    memory_type,
                    content,
                    _json(candidate["source_event_ids"]),
                    _json(candidate["source_session_ids"]),
                    _json(candidate["source_task_ids"]),
                    _json({"candidate_id": candidate_id, "reason": candidate.get("reason", "")}),
                    candidate["confidence"],
                    now,
                    now,
                    now,
                    "active",
                    _json(supersede_ids),
                    "[]",
                    "[]",
                    candidate["canonical_fingerprint"],
                    2,
                ),
            )
            conn.execute(
                "UPDATE memory_candidates SET status='committed',updated_at=? WHERE id=?",
                (now, candidate_id),
            )
            for old_id in supersede_ids:
                conn.execute(
                    "UPDATE memory_records SET status='superseded',superseded_by=?,updated_at=? WHERE id=? AND status='active'",
                    (_json([record_id]), now, old_id),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO memory_edges(id,source_id,target_id,edge_type,created_at) VALUES(?,?,?,?,?)",
                    (_new_id("medge"), old_id, record_id, "supersedes", now),
                )
            self._event_sqlite(
                conn,
                "memory.committed",
                {"memory_id": record_id, "candidate_id": candidate_id, "supersedes": supersede_ids},
                trace_id=trace_id,
                session_id=None,
                task_id=None,
                workspace_id=scope_id if scope_type == "workspace" else None,
                idempotency_key=f"memory-committed:{candidate_id}",
            )
            row = conn.execute("SELECT * FROM memory_records WHERE id=?", (record_id,)).fetchone()
            conn.commit()
            assert row is not None
            return {
                "status": "committed",
                "candidate_id": candidate_id,
                "record": self._memory_out(dict(row)),
            }
        except BaseException:
            conn.rollback()
            raise
        finally:
            if conn is not self._sqlite_memory:
                conn.close()

    async def search_memory(
        self,
        query: str = "",
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 20,
        min_confidence: float = 0.0,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        limit = max(1, min(int(limit), 100))
        statuses = (
            ("active", "superseded", "invalidated", "forgotten")
            if include_superseded
            else ("active",)
        )
        pattern = f"%{query}%"
        if self.backend == "sqlite":

            def read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
                clauses = [f"status IN ({','.join('?' for _ in statuses)})", "confidence>=?"]
                params: list[Any] = [*statuses, float(min_confidence)]
                if scope_type:
                    clauses.append("scope_type=?")
                    params.append(scope_type)
                if scope_id:
                    clauses.append("scope_id=?")
                    params.append(scope_id)
                if memory_type:
                    clauses.append("memory_type=?")
                    params.append(memory_type)
                if query:
                    clauses.append("(content LIKE ? OR tags LIKE ?)")
                    params.extend([pattern, pattern])
                rows = conn.execute(
                    "SELECT * FROM memory_records WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY confidence DESC,updated_at DESC LIMIT ?",
                    (*params, limit),
                ).fetchall()
                return [self._memory_out(dict(row)) for row in rows]

            return await asyncio.to_thread(lambda: self._read_sqlite(read))
        clauses = ["status = ANY($1::text[])", "confidence >= $2"]
        params: list[Any] = [list(statuses), float(min_confidence)]
        idx = 3
        if scope_type:
            clauses.append(f"scope_type=${idx}")
            params.append(scope_type)
            idx += 1
        if scope_id:
            clauses.append(f"scope_id=${idx}")
            params.append(scope_id)
            idx += 1
        if memory_type:
            clauses.append(f"memory_type=${idx}")
            params.append(memory_type)
            idx += 1
        if query:
            clauses.append(f"(content ILIKE ${idx} OR tags ILIKE ${idx})")
            params.append(pattern)
            idx += 1
        params.append(limit)
        rows = await self._pool.fetch(
            "SELECT * FROM memory_records WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY confidence DESC,updated_at DESC LIMIT ${idx}",
            *params,
        )
        return [self._memory_out(dict(row)) for row in rows]

    def _read_sqlite(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        conn = self._sqlite_connection()
        try:
            return fn(conn)
        finally:
            if conn is not self._sqlite_memory:
                conn.close()

    async def get_memory(
        self, memory_id: str, *, include_sources: bool = False
    ) -> dict[str, Any] | None:
        await self._ensure_connected()
        if self.backend == "sqlite":
            row = await asyncio.to_thread(
                lambda: self._read_sqlite(
                    lambda c: c.execute(
                        "SELECT * FROM memory_records WHERE id=?", (memory_id,)
                    ).fetchone()
                )
            )
        else:
            row = await self._pool.fetchrow("SELECT * FROM memory_records WHERE id=$1", memory_id)
        if row is None:
            return None
        out = self._memory_out(dict(row))
        if include_sources:
            out["sources"] = await self.show_memory_source(memory_id)
        return out

    async def get_memory_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        """Return a candidate through the same durable authority as records."""
        await self._ensure_connected()
        if self.backend == "sqlite":
            row = await asyncio.to_thread(
                lambda: self._read_sqlite(
                    lambda c: c.execute(
                        "SELECT * FROM memory_candidates WHERE id=?", (candidate_id,)
                    ).fetchone()
                )
            )
        else:
            row = await self._pool.fetchrow(
                "SELECT * FROM memory_candidates WHERE id=$1", candidate_id
            )
        return self._candidate_out(dict(row)) if row is not None else None

    async def show_memory_source(self, memory_id: str) -> dict[str, Any] | None:
        record = await self.get_memory(memory_id)
        if record is None:
            return None
        await self._ensure_connected()
        event_ids = record.get("source_event_ids", [])
        if not event_ids:
            return {
                "memory_id": memory_id,
                "provenance": record.get("provenance_json", {}),
                "events": [],
            }
        if self.backend == "sqlite":

            def read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
                rows = conn.execute(
                    f"SELECT * FROM personal_events WHERE id IN ({','.join('?' for _ in event_ids)}) ORDER BY occurred_at",
                    event_ids,
                ).fetchall()
                return [dict(row) for row in rows]

            events = await asyncio.to_thread(lambda: self._read_sqlite(read))
        else:
            rows = await self._pool.fetch(
                "SELECT * FROM personal_events WHERE id = ANY($1::text[]) ORDER BY occurred_at",
                event_ids,
            )
            events = [dict(row) for row in rows]
        for event in events:
            event["payload"] = _loads(event.pop("payload_json", "{}"), {})
        return {
            "memory_id": memory_id,
            "provenance": record.get("provenance_json", {}),
            "events": events,
            "missing_event_ids": sorted(set(event_ids) - {str(e["id"]) for e in events}),
        }

    async def correct_memory(
        self,
        memory_id: str,
        content: str,
        *,
        source_event_ids: Iterable[str] = (),
        source_session_ids: Iterable[str] = (),
        source_task_ids: Iterable[str] = (),
        reason: str = "user_correction",
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        old = await self.get_memory(memory_id)
        if old is None:
            raise PersonalRuntimeError("NOT_FOUND", memory_id)
        source_session_ids = list(source_session_ids)
        source_task_ids = list(source_task_ids)
        source_event_ids = list(source_event_ids)
        if not source_event_ids:
            source = await self.record_event(
                "memory.correction_source",
                {"memory_id": memory_id, "content": content},
                trace_id=trace_id,
                session_id=source_session_ids[0] if source_session_ids else None,
                task_id=source_task_ids[0] if source_task_ids else None,
                workspace_id=old["scope_id"] if old["scope_type"] == "workspace" else None,
                idempotency_key=f"memory-correction-source:{memory_id}:{canonical_fingerprint(content)}",
            )
            source_event_ids = [source["id"]]
        candidate = await self.create_memory_candidate(
            content,
            scope_type=old["scope_type"],
            scope_id=old["scope_id"],
            memory_type=old["memory_type"],
            source_event_ids=source_event_ids or old.get("source_event_ids", []),
            source_session_ids=source_session_ids,
            source_task_ids=source_task_ids,
            confidence=max(float(old.get("confidence", 0.5)), 0.8),
            reason=reason,
            provenance={"corrected_from": memory_id},
            trace_id=trace_id,
        )
        result = await self.commit_memory_candidate(
            candidate["id"], allow_conflicts=True, supersedes=[memory_id], trace_id=trace_id
        )
        await self.record_event(
            "memory.corrected",
            {"old_id": memory_id, "new_id": result["record"]["id"]},
            trace_id=trace_id,
            session_id=(source_session_ids or [None])[0],
            task_id=(source_task_ids or [None])[0],
            workspace_id=old["scope_id"] if old["scope_type"] == "workspace" else None,
            idempotency_key=f"memory-corrected:{memory_id}:{result['record']['id']}",
        )
        await self.record_event(
            "memory.superseded",
            {"old_id": memory_id, "new_id": result["record"]["id"]},
            trace_id=trace_id,
            session_id=(source_session_ids or [None])[0],
            task_id=(source_task_ids or [None])[0],
            workspace_id=old["scope_id"] if old["scope_type"] == "workspace" else None,
            idempotency_key=f"memory-superseded:{memory_id}:{result['record']['id']}",
        )
        return {
            "status": "corrected",
            "old_id": memory_id,
            "new_id": result["record"]["id"],
            "record": result["record"],
        }

    async def forget_memory(self, memory_id: str, *, trace_id: str | None = None) -> dict[str, Any]:
        await self._ensure_connected()
        now = _now()
        if self.backend == "sqlite":

            def tx(conn: sqlite3.Connection) -> dict[str, Any]:
                row = conn.execute(
                    "SELECT * FROM memory_records WHERE id=?", (memory_id,)
                ).fetchone()
                if row is None:
                    raise PersonalRuntimeError("NOT_FOUND", memory_id)
                conn.execute(
                    "UPDATE memory_records SET status='forgotten',updated_at=? WHERE id=?",
                    (now, memory_id),
                )
                self._event_sqlite(
                    conn,
                    "memory.forgotten",
                    {"memory_id": memory_id},
                    trace_id=trace_id,
                    session_id=None,
                    task_id=None,
                    workspace_id=None,
                    idempotency_key=f"memory-forgotten:{memory_id}",
                )
                return {"status": "forgotten", "memory_id": memory_id}

            return await asyncio.to_thread(self._sqlite_tx, tx)

        async def tx(conn: Any) -> dict[str, Any]:
            row = await conn.fetchrow(
                "SELECT id FROM memory_records WHERE id=$1 FOR UPDATE", memory_id
            )
            if row is None:
                raise PersonalRuntimeError("NOT_FOUND", memory_id)
            await conn.execute(
                "UPDATE memory_records SET status='forgotten',updated_at=$1 WHERE id=$2",
                now,
                memory_id,
            )
            await self._event_pg(
                conn,
                "memory.forgotten",
                {"memory_id": memory_id},
                trace_id=trace_id,
                session_id=None,
                task_id=None,
                workspace_id=None,
                idempotency_key=f"memory-forgotten:{memory_id}",
            )
            return {"status": "forgotten", "memory_id": memory_id}

        return await self._pg_tx(tx)

    async def memory_doctor(self) -> dict[str, Any]:
        await self._ensure_connected()
        if self.backend == "sqlite":

            def read(conn: sqlite3.Connection) -> dict[str, Any]:
                rows = [dict(r) for r in conn.execute("SELECT * FROM memory_records").fetchall()]
                events = {
                    str(r[0]) for r in conn.execute("SELECT id FROM personal_events").fetchall()
                }
                candidates = [
                    dict(r) for r in conn.execute("SELECT * FROM memory_candidates").fetchall()
                ]
                return self._doctor(rows, candidates, events)

            return await asyncio.to_thread(lambda: self._read_sqlite(read))
        rows = [dict(r) for r in await self._pool.fetch("SELECT * FROM memory_records")]
        candidates = [dict(r) for r in await self._pool.fetch("SELECT * FROM memory_candidates")]
        events = {str(r["id"]) for r in await self._pool.fetch("SELECT id FROM personal_events")}
        return self._doctor(rows, candidates, events)

    def _doctor(
        self, rows: list[dict[str, Any]], candidates: list[dict[str, Any]], events: set[str]
    ) -> dict[str, Any]:
        active = [r for r in rows if r.get("status") == "active"]
        fingerprints = [r.get("canonical_fingerprint") for r in active]
        duplicate_groups = len(fingerprints) - len(set(fingerprints))
        missing = [
            str(r["id"])
            for r in rows
            if any(str(e) not in events for e in _loads(r.get("source_event_ids"), []))
        ]
        invalid_scope = [
            str(r["id"])
            for r in rows
            if r.get("scope_type") not in _VALID_SCOPE_TYPES or not r.get("scope_id")
        ]
        low_confidence = [str(r["id"]) for r in active if float(r.get("confidence", 0)) < 0.5]
        graph = {str(r["id"]): _loads(r.get("superseded_by"), []) for r in rows}
        cycles: list[str] = []
        for start in graph:
            seen: set[str] = set()
            cur = start
            while cur and cur not in seen:
                seen.add(cur)
                nxt = graph.get(cur, [])
                cur = str(nxt[0]) if nxt else ""
            if cur == start:
                cycles.append(start)
        return {
            "records": len(rows),
            "active": len(active),
            "candidates": len(candidates),
            "duplicate_rate": (duplicate_groups / len(active) if active else 0.0),
            "duplicate_groups": max(0, duplicate_groups),
            "orphan_provenance": missing,
            "missing_source_events": missing,
            "low_confidence_active": low_confidence,
            "invalid_scope": invalid_scope,
            "supersede_chain_cycles": cycles,
            "stale_index": False,
            "embedding_index_drift": False,
            "provenance_coverage": (
                sum(
                    bool(
                        _loads(r.get("source_event_ids"), [])
                        or _loads(r.get("source_session_ids"), [])
                        or _loads(r.get("source_task_ids"), [])
                    )
                    for r in rows
                )
                / len(rows)
                if rows
                else 1.0
            ),
        }

    # ── skills ───────────────────────────────────────────────────────
    @staticmethod
    def _skill_version_out(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for key in (
            "trigger_examples",
            "parameters_schema",
            "source_event_ids",
            "source_task_ids",
            "safety_manifest",
            "safety_scan",
        ):
            out[key] = _loads(
                out.get(key), {} if key.endswith(("schema", "manifest", "scan")) else []
            )
        out["success_rate"] = out["success_count"] / max(
            1, out["success_count"] + out["failure_count"] + out["partial_count"]
        )
        return out

    async def create_skill_candidate(
        self,
        name: str,
        description: str,
        *,
        scope_type: str,
        scope_id: str,
        trigger_examples: Iterable[str] = (),
        parameters_schema: dict[str, Any] | None = None,
        execution_type: str = "prompt",
        execution_ref: str = "",
        source_event_ids: Iterable[str] = (),
        source_task_ids: Iterable[str] = (),
        created_by: str = "user",
        parent_version: int | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        if scope_type not in {"user", "workspace"}:
            raise PersonalRuntimeError("INVALID_SCOPE", scope_type)
        if execution_type not in {"prompt", "tool_chain", "python", "external"}:
            raise PersonalRuntimeError("INVALID_EXECUTION_TYPE", execution_type)
        if not name.strip() or not description.strip():
            raise PersonalRuntimeError("INVALID_CONTENT", "skill name/description required")
        trigger_examples = list(trigger_examples)
        source_event_ids = list(source_event_ids)
        source_task_ids = list(source_task_ids)
        await self._ensure_connected()
        skill_id, version_id, now = _new_id("skill"), _new_id("skill_version"), _now()
        if self.backend == "sqlite":

            def tx(conn: sqlite3.Connection) -> dict[str, Any]:
                row = conn.execute(
                    "SELECT * FROM skill_records WHERE name=? AND scope_type=? AND scope_id=?",
                    (name, scope_type, scope_id),
                ).fetchone()
                if row is None:
                    skill_id_local = skill_id
                    conn.execute(
                        "INSERT INTO skill_records(id,name,scope_type,scope_id,current_version,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (skill_id_local, name, scope_type, scope_id, 0, "candidate", now, now),
                    )
                else:
                    skill_id_local = row["id"]
                maxv = conn.execute(
                    "SELECT COALESCE(MAX(version),0) FROM skill_versions WHERE skill_id=?",
                    (skill_id_local,),
                ).fetchone()[0]
                version = (
                    int(parent_version or maxv) + 1 if parent_version is not None or maxv else 1
                )
                conn.execute(
                    "INSERT INTO skill_versions(id,skill_id,version,description,trigger_examples,parameters_schema,execution_type,execution_ref,source_event_ids,source_task_ids,created_by,created_at,updated_at,trust_status,status,safety_manifest,safety_scan,parent_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        version_id,
                        skill_id_local,
                        version,
                        description,
                        _json(trigger_examples),
                        _json(parameters_schema or {"type": "object", "properties": {}}),
                        execution_type,
                        execution_ref,
                        _json(source_event_ids),
                        _json(source_task_ids),
                        created_by,
                        now,
                        now,
                        "review_required",
                        "candidate",
                        _json({"capabilities": []}),
                        _json({"verdict": "pending", "advisory": True}),
                        parent_version or (maxv if maxv else None),
                    ),
                )
                event = self._event_sqlite(
                    conn,
                    "skill.candidate_created",
                    {
                        "skill_id": skill_id_local,
                        "version": version,
                        "name": name,
                        "description": description,
                    },
                    trace_id=trace_id,
                    session_id=None,
                    task_id=None,
                    workspace_id=scope_id if scope_type == "workspace" else None,
                    idempotency_key=f"skill-candidate:{version_id}",
                )
                version_event = self._event_sqlite(
                    conn,
                    "skill.version_created",
                    {"skill_id": skill_id_local, "version": version, "version_id": version_id},
                    trace_id=trace_id,
                    session_id=None,
                    task_id=None,
                    workspace_id=scope_id if scope_type == "workspace" else None,
                    idempotency_key=f"skill-version-created:{version_id}",
                )
                if not source_event_ids:
                    source_event_ids.extend([event["id"], version_event["id"]])
                    conn.execute(
                        "UPDATE skill_versions SET source_event_ids=?,updated_at=? WHERE id=?",
                        (_json(source_event_ids), _now(), version_id),
                    )
                row2 = conn.execute(
                    "SELECT * FROM skill_versions WHERE id=?", (version_id,)
                ).fetchone()
                assert row2 is not None
                return self._skill_version_out(dict(row2)) | {
                    "skill_id": skill_id_local,
                    "event_id": event["id"],
                }

            return await asyncio.to_thread(self._sqlite_tx, tx)

        async def tx(conn: Any) -> dict[str, Any]:
            row = await conn.fetchrow(
                "SELECT * FROM skill_records WHERE name=$1 AND scope_type=$2 AND scope_id=$3",
                name,
                scope_type,
                scope_id,
            )
            if row is None:
                await conn.execute(
                    "INSERT INTO skill_records(id,name,scope_type,scope_id,current_version,status,created_at,updated_at) VALUES($1,$2,$3,$4,0,$5,$6,$6)",
                    skill_id,
                    name,
                    scope_type,
                    scope_id,
                    "candidate",
                    now,
                )
                skill_id_local = skill_id
            else:
                skill_id_local = row["id"]
            maxv = await conn.fetchval(
                "SELECT COALESCE(MAX(version),0) FROM skill_versions WHERE skill_id=$1",
                skill_id_local,
            )
            version = int(parent_version or maxv) + 1 if parent_version is not None or maxv else 1
            await conn.execute(
                "INSERT INTO skill_versions(id,skill_id,version,description,trigger_examples,parameters_schema,execution_type,execution_ref,source_event_ids,source_task_ids,created_by,created_at,updated_at,trust_status,status,safety_manifest,safety_scan,parent_version) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$12,$13,$14,$15,$16,$17)",
                version_id,
                skill_id_local,
                version,
                description,
                _json(trigger_examples),
                _json(parameters_schema or {"type": "object", "properties": {}}),
                execution_type,
                execution_ref,
                _json(source_event_ids),
                _json(source_task_ids),
                created_by,
                now,
                "review_required",
                "candidate",
                _json({"capabilities": []}),
                _json({"verdict": "pending", "advisory": True}),
                parent_version or (maxv if maxv else None),
            )
            event = await self._event_pg(
                conn,
                "skill.candidate_created",
                {
                    "skill_id": skill_id_local,
                    "version": version,
                    "name": name,
                    "description": description,
                },
                trace_id=trace_id,
                session_id=None,
                task_id=None,
                workspace_id=scope_id if scope_type == "workspace" else None,
                idempotency_key=f"skill-candidate:{version_id}",
            )
            version_event = await self._event_pg(
                conn,
                "skill.version_created",
                {"skill_id": skill_id_local, "version": version, "version_id": version_id},
                trace_id=trace_id,
                session_id=None,
                task_id=None,
                workspace_id=scope_id if scope_type == "workspace" else None,
                idempotency_key=f"skill-version-created:{version_id}",
            )
            if not source_event_ids:
                source_event_ids.extend([event["id"], version_event["id"]])
                await conn.execute(
                    "UPDATE skill_versions SET source_event_ids=$1,updated_at=$2 WHERE id=$3",
                    _json(source_event_ids),
                    _now(),
                    version_id,
                )
            row2 = await conn.fetchrow("SELECT * FROM skill_versions WHERE id=$1", version_id)
            if row2 is None:
                raise PersonalRuntimeError("SKILL_FAILED", "candidate was not visible")
            return self._skill_version_out(dict(row2)) | {
                "skill_id": skill_id_local,
                "event_id": event["id"],
            }

        return await self._pg_tx(tx)

    @staticmethod
    def _static_skill_scan(version: dict[str, Any]) -> dict[str, Any]:
        source = "\n".join(
            [str(version.get("description", "")), str(version.get("execution_ref", ""))]
        ).lower()
        findings = [
            token
            for token in (
                "rm -rf",
                "subprocess",
                "os.system(",
                "eval(",
                "exec(",
                "__import__",
                "curl ",
            )
            if token in source
        ]
        return {
            "verdict": "blocked" if findings else "pass",
            "findings": findings,
            "authority": "deterministic_manifest_and_sandbox",
            "semantic_advisory": "not_run",
        }

    async def confirm_skill(
        self, skill_version_id: str, *, trace_id: str | None = None
    ) -> dict[str, Any]:
        await self._ensure_connected()
        if self.backend == "sqlite":

            def tx(conn: sqlite3.Connection) -> dict[str, Any]:
                row = conn.execute(
                    "SELECT sv.*,sr.name,sr.scope_type,sr.scope_id FROM skill_versions sv JOIN skill_records sr ON sr.id=sv.skill_id WHERE sv.id=?",
                    (skill_version_id,),
                ).fetchone()
                if row is None:
                    raise PersonalRuntimeError("NOT_FOUND", skill_version_id)
                version = self._skill_version_out(dict(row))
                scan = self._static_skill_scan(version)
                if scan["verdict"] != "pass":
                    conn.execute(
                        "UPDATE skill_versions SET trust_status='blocked',status='blocked',safety_scan=?,updated_at=? WHERE id=?",
                        (_json(scan), _now(), skill_version_id),
                    )
                    raise PersonalRuntimeError("SAFETY_HOLD", ",".join(scan["findings"]))
                now = _now()
                conn.execute(
                    "UPDATE skill_versions SET trust_status='trusted',status='active',safety_scan=?,updated_at=? WHERE id=?",
                    (_json(scan), now, skill_version_id),
                )
                conn.execute(
                    "UPDATE skill_versions SET status='deprecated' WHERE skill_id=? AND id<>? AND status='active'",
                    (row["skill_id"], skill_version_id),
                )
                conn.execute(
                    "UPDATE skill_records SET current_version=?,status='active',updated_at=? WHERE id=?",
                    (row["version"], now, row["skill_id"]),
                )
                self._event_sqlite(
                    conn,
                    "skill.created",
                    {"skill_id": row["skill_id"], "version": row["version"], "scan": scan},
                    trace_id=trace_id,
                    session_id=None,
                    task_id=None,
                    workspace_id=row["scope_id"] if row["scope_type"] == "workspace" else None,
                    idempotency_key=f"skill-confirmed:{skill_version_id}",
                )
                return self._skill_version_out(
                    dict(
                        conn.execute(
                            "SELECT * FROM skill_versions WHERE id=?", (skill_version_id,)
                        ).fetchone()
                    )
                ) | {"skill_id": row["skill_id"]}

            return await asyncio.to_thread(self._sqlite_tx, tx)

        async def tx(conn: Any) -> dict[str, Any]:
            row = await conn.fetchrow(
                "SELECT sv.*,sr.name,sr.scope_type,sr.scope_id FROM skill_versions sv JOIN skill_records sr ON sr.id=sv.skill_id WHERE sv.id=$1 FOR UPDATE",
                skill_version_id,
            )
            if row is None:
                raise PersonalRuntimeError("NOT_FOUND", skill_version_id)
            version = self._skill_version_out(dict(row))
            scan = self._static_skill_scan(version)
            if scan["verdict"] != "pass":
                await conn.execute(
                    "UPDATE skill_versions SET trust_status='blocked',status='blocked',safety_scan=$1,updated_at=$2 WHERE id=$3",
                    _json(scan),
                    _now(),
                    skill_version_id,
                )
                raise PersonalRuntimeError("SAFETY_HOLD", ",".join(scan["findings"]))
            now = _now()
            await conn.execute(
                "UPDATE skill_versions SET trust_status='trusted',status='active',safety_scan=$1,updated_at=$2 WHERE id=$3",
                _json(scan),
                now,
                skill_version_id,
            )
            await conn.execute(
                "UPDATE skill_versions SET status='deprecated' WHERE skill_id=$1 AND id<>$2 AND status='active'",
                row["skill_id"],
                skill_version_id,
            )
            await conn.execute(
                "UPDATE skill_records SET current_version=$1,status='active',updated_at=$2 WHERE id=$3",
                row["version"],
                now,
                row["skill_id"],
            )
            await self._event_pg(
                conn,
                "skill.created",
                {"skill_id": row["skill_id"], "version": row["version"], "scan": scan},
                trace_id=trace_id,
                session_id=None,
                task_id=None,
                workspace_id=row["scope_id"] if row["scope_type"] == "workspace" else None,
                idempotency_key=f"skill-confirmed:{skill_version_id}",
            )
            result = await conn.fetchrow(
                "SELECT * FROM skill_versions WHERE id=$1", skill_version_id
            )
            assert result is not None
            return self._skill_version_out(dict(result)) | {"skill_id": row["skill_id"]}

        return await self._pg_tx(tx)

    async def search_skills(
        self,
        query: str = "",
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
        include_candidates: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        limit = max(1, min(int(limit), 100))
        pattern = f"%{query}%"
        states = ("active", "candidate") if include_candidates else ("active",)
        if self.backend == "sqlite":

            def read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
                clauses = [f"sv.status IN ({','.join('?' for _ in states)})"]
                params = list(states)
                if scope_type:
                    clauses.append("sr.scope_type=?")
                    params.append(scope_type)
                if scope_id:
                    clauses.append("sr.scope_id=?")
                    params.append(scope_id)
                if query:
                    clauses.append("(sr.name LIKE ? OR sv.description LIKE ?)")
                    params.extend([pattern, pattern])
                rows = conn.execute(
                    "SELECT sr.id AS skill_record_id,sr.name,sr.scope_type,sr.scope_id,sr.current_version,sr.status AS skill_record_status,sr.created_at AS skill_record_created_at,sr.updated_at AS skill_record_updated_at,sv.id,sv.skill_id,sv.version,sv.description,sv.trigger_examples,sv.parameters_schema,sv.execution_type,sv.execution_ref,sv.source_event_ids,sv.source_task_ids,sv.created_by,sv.created_at,sv.updated_at,sv.trust_status,sv.status,sv.safety_manifest,sv.safety_scan,sv.parent_version,sv.success_count,sv.failure_count,sv.partial_count,sv.last_run_at,sv.last_success_at FROM skill_records sr JOIN skill_versions sv ON sv.skill_id=sr.id AND sv.version=CASE WHEN sv.status='active' THEN sr.current_version ELSE sv.version END WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY sr.updated_at DESC LIMIT ?",
                    (*params, limit),
                ).fetchall()
                return [self._skill_version_out(dict(r)) for r in rows]

            return await asyncio.to_thread(lambda: self._read_sqlite(read))
        clauses = ["sv.status = ANY($1::text[])"]
        params: [Any] = [list(states)]
        idx = 2
        if scope_type:
            clauses.append(f"sr.scope_type=${idx}")
            params.append(scope_type)
            idx += 1
        if scope_id:
            clauses.append(f"sr.scope_id=${idx}")
            params.append(scope_id)
            idx += 1
        if query:
            clauses.append(f"(sr.name ILIKE ${idx} OR sv.description ILIKE ${idx})")
            params.append(pattern)
            idx += 1
        params.append(limit)
        rows = await self._pool.fetch(
            "SELECT sr.id AS skill_record_id,sr.name,sr.scope_type,sr.scope_id,sr.current_version,sr.status AS skill_record_status,sr.created_at AS skill_record_created_at,sr.updated_at AS skill_record_updated_at,sv.id,sv.skill_id,sv.version,sv.description,sv.trigger_examples,sv.parameters_schema,sv.execution_type,sv.execution_ref,sv.source_event_ids,sv.source_task_ids,sv.created_by,sv.created_at,sv.updated_at,sv.trust_status,sv.status,sv.safety_manifest,sv.safety_scan,sv.parent_version,sv.success_count,sv.failure_count,sv.partial_count,sv.last_run_at,sv.last_success_at FROM skill_records sr JOIN skill_versions sv ON sv.skill_id=sr.id AND sv.version=CASE WHEN sv.status='active' THEN sr.current_version ELSE sv.version END WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY sr.updated_at DESC LIMIT ${idx}",
            *params,
        )
        return [self._skill_version_out(dict(r)) for r in rows]

    async def get_skill(self, skill_id: str, *, versions: bool = False) -> dict[str, Any] | None:
        await self._ensure_connected()
        if self.backend == "sqlite":
            row = await asyncio.to_thread(
                lambda: self._read_sqlite(
                    lambda c: c.execute(
                        "SELECT * FROM skill_records WHERE id=?", (skill_id,)
                    ).fetchone()
                )
            )
        else:
            row = await self._pool.fetchrow("SELECT * FROM skill_records WHERE id=$1", skill_id)
        if row is None:
            return None
        out = dict(row)
        if self.backend == "sqlite":
            rows = await asyncio.to_thread(
                lambda: self._read_sqlite(
                    lambda c: c.execute(
                        "SELECT * FROM skill_versions WHERE skill_id=? ORDER BY version DESC",
                        (skill_id,),
                    ).fetchall()
                )
            )
        else:
            rows = await self._pool.fetch(
                "SELECT * FROM skill_versions WHERE skill_id=$1 ORDER BY version DESC", skill_id
            )
        if versions:
            out["versions"] = [self._skill_version_out(dict(r)) for r in rows]
        else:
            current = next(
                (r for r in rows if int(r["version"]) == int(out["current_version"])),
                rows[0] if rows else None,
            )
            out["versions"] = [self._skill_version_out(dict(current))] if current else []
        return out

    async def get_skill_version(self, skill_version_id: str) -> dict[str, Any] | None:
        """Resolve a version id with its owning skill scope for API/tool gates."""
        await self._ensure_connected()
        if self.backend == "sqlite":
            row = await asyncio.to_thread(
                lambda: self._read_sqlite(
                    lambda c: c.execute(
                        "SELECT sv.*,sr.name,sr.scope_type,sr.scope_id,sr.current_version,sr.status AS skill_record_status FROM skill_versions sv JOIN skill_records sr ON sr.id=sv.skill_id WHERE sv.id=?",
                        (skill_version_id,),
                    ).fetchone()
                )
            )
        else:
            row = await self._pool.fetchrow(
                "SELECT sv.*,sr.name,sr.scope_type,sr.scope_id,sr.current_version,sr.status AS skill_record_status FROM skill_versions sv JOIN skill_records sr ON sr.id=sv.skill_id WHERE sv.id=$1",
                skill_version_id,
            )
        return self._skill_version_out(dict(row)) if row is not None else None

    async def deprecate_skill(
        self, skill_id: str, *, trace_id: str | None = None
    ) -> dict[str, Any]:
        """Disable a skill without deleting its auditable version history."""
        await self._ensure_connected()
        now = _now()
        if self.backend == "sqlite":

            def tx(conn: sqlite3.Connection) -> dict[str, Any]:
                row = conn.execute(
                    "SELECT id FROM skill_records WHERE id=?", (skill_id,)
                ).fetchone()
                if row is None:
                    raise PersonalRuntimeError("NOT_FOUND", skill_id)
                conn.execute(
                    "UPDATE skill_records SET status='deprecated',updated_at=? WHERE id=?",
                    (now, skill_id),
                )
                conn.execute(
                    "UPDATE skill_versions SET status='deprecated',updated_at=? WHERE skill_id=? AND status='active'",
                    (now, skill_id),
                )
                self._event_sqlite(
                    conn,
                    "skill.deprecated",
                    {"skill_id": skill_id},
                    trace_id=trace_id,
                    session_id=None,
                    task_id=None,
                    workspace_id=None,
                    idempotency_key=f"skill-deprecated:{skill_id}",
                )
                return {"status": "deprecated", "skill_id": skill_id}

            return await asyncio.to_thread(self._sqlite_tx, tx)

        async def tx(conn: Any) -> dict[str, Any]:
            row = await conn.fetchrow(
                "SELECT id FROM skill_records WHERE id=$1 FOR UPDATE", skill_id
            )
            if row is None:
                raise PersonalRuntimeError("NOT_FOUND", skill_id)
            await conn.execute(
                "UPDATE skill_records SET status='deprecated',updated_at=$1 WHERE id=$2",
                now,
                skill_id,
            )
            await conn.execute(
                "UPDATE skill_versions SET status='deprecated',updated_at=$1 WHERE skill_id=$2 AND status='active'",
                now,
                skill_id,
            )
            await self._event_pg(
                conn,
                "skill.deprecated",
                {"skill_id": skill_id},
                trace_id=trace_id,
                session_id=None,
                task_id=None,
                workspace_id=None,
                idempotency_key=f"skill-deprecated:{skill_id}",
            )
            return {"status": "deprecated", "skill_id": skill_id}

        return await self._pg_tx(tx)

    async def run_skill(
        self,
        skill_id: str,
        params: dict[str, Any] | None = None,
        *,
        task_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a trusted prompt skill or delegate a named loaded skill.

        Prompt skills return their declared instructions to MasterAgent; they
        do not silently mutate the prompt or execute hidden behavior.  Named
        executable skills reuse SkillHub's existing sandbox/permission gate.
        """
        skill = await self.get_skill(skill_id)
        if skill is None or not skill.get("versions"):
            raise PersonalRuntimeError("NOT_FOUND", skill_id)
        version = skill["versions"][0]
        if version.get("status") != "active" or version.get("trust_status") != "trusted":
            raise PersonalRuntimeError("SAFETY_HOLD", f"skill {skill_id} is not trusted/active")
        params = dict(params or {})
        started = time.monotonic()
        result: dict[str, Any]
        status = "complete"
        try:
            if version.get("execution_type") == "prompt":
                result = {
                    "execution_type": "prompt",
                    "instructions": version["description"],
                    "parameters": params,
                }
            elif version.get("execution_ref"):
                from server.skill_hub import skill_hub

                if not skill_hub.has(str(version["execution_ref"])):
                    raise PersonalRuntimeError(
                        "NOT_FOUND", f"loaded skill {version['execution_ref']}"
                    )
                raw = await skill_hub.execute(str(version["execution_ref"]), params)
                result = {"execution_type": version["execution_type"], "value": raw}
            else:
                raise PersonalRuntimeError("INVALID_EXECUTION", "skill has no execution reference")
        except Exception:
            status = "failed"
            raise
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            await self.record_skill_run(
                skill_id,
                int(version["version"]),
                task_id=task_id,
                trace_id=trace_id,
                input_params=params,
                result_status=status,
                duration_ms=duration_ms,
                evidence=[
                    {"version": version["version"], "execution_type": version["execution_type"]}
                ],
            )
        return {
            "skill_id": skill_id,
            "version": version["version"],
            "result_status": status,
            "result": result,
        }

    async def record_skill_run(
        self,
        skill_id: str,
        version: int,
        *,
        task_id: str | None,
        trace_id: str | None,
        input_params: dict[str, Any],
        result_status: str,
        acceptance_result: dict[str, Any] | None = None,
        duration_ms: int = 0,
        cost_usd: float = 0.0,
        artifacts: list[Any] = (),
        evidence: list[Any] = (),
    ) -> dict[str, Any]:
        if result_status not in {"complete", "partial", "failed"}:
            raise PersonalRuntimeError("INVALID_RESULT", result_status)
        await self._ensure_connected()
        run_id = _new_id("skill_run")
        now = _now()
        if self.backend == "sqlite":

            def tx(conn: sqlite3.Connection) -> dict[str, Any]:
                conn.execute(
                    "INSERT INTO skill_runs(id,skill_id,version,task_id,trace_id,input_params,result_status,acceptance_result,duration_ms,cost_usd,artifacts,evidence,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        skill_id,
                        version,
                        task_id,
                        trace_id,
                        _json(input_params),
                        result_status,
                        _json(acceptance_result or {}),
                        int(duration_ms),
                        float(cost_usd),
                        _json(list(artifacts)),
                        _json(list(evidence)),
                        now,
                    ),
                )
                field = {
                    "complete": "success_count",
                    "partial": "partial_count",
                    "failed": "failure_count",
                }[result_status]
                conn.execute(
                    f"UPDATE skill_versions SET {field}={field}+1,last_run_at=?,last_success_at=CASE WHEN ?='complete' THEN ? ELSE last_success_at END,updated_at=? WHERE skill_id=? AND version=?",
                    (now, result_status, now, now, skill_id, version),
                )
                self._event_sqlite(
                    conn,
                    "skill.executed",
                    {
                        "skill_id": skill_id,
                        "version": version,
                        "run_id": run_id,
                        "result_status": result_status,
                    },
                    trace_id=trace_id,
                    session_id=None,
                    task_id=task_id,
                    workspace_id=None,
                    idempotency_key=f"skill-run:{run_id}",
                )
                self._event_sqlite(
                    conn,
                    f"skill.{result_status}",
                    {"skill_id": skill_id, "version": version, "run_id": run_id},
                    trace_id=trace_id,
                    session_id=None,
                    task_id=task_id,
                    workspace_id=None,
                    idempotency_key=f"skill-run-status:{run_id}:{result_status}",
                )
                return {
                    "run_id": run_id,
                    "skill_id": skill_id,
                    "version": version,
                    "result_status": result_status,
                    "duration_ms": duration_ms,
                    "cost_usd": cost_usd,
                }

            return await asyncio.to_thread(self._sqlite_tx, tx)

        async def tx(conn: Any) -> dict[str, Any]:
            await conn.execute(
                "INSERT INTO skill_runs(id,skill_id,version,task_id,trace_id,input_params,result_status,acceptance_result,duration_ms,cost_usd,artifacts,evidence,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                run_id,
                skill_id,
                version,
                task_id,
                trace_id,
                _json(input_params),
                result_status,
                _json(acceptance_result or {}),
                int(duration_ms),
                float(cost_usd),
                _json(list(artifacts)),
                _json(list(evidence)),
                now,
            )
            field = {
                "complete": "success_count",
                "partial": "partial_count",
                "failed": "failure_count",
            }[result_status]
            await conn.execute(
                f"UPDATE skill_versions SET {field}={field}+1,last_run_at=$1,last_success_at=CASE WHEN $2='complete' THEN $1 ELSE last_success_at END,updated_at=$1 WHERE skill_id=$3 AND version=$4",
                now,
                result_status,
                skill_id,
                version,
            )
            await self._event_pg(
                conn,
                "skill.executed",
                {
                    "skill_id": skill_id,
                    "version": version,
                    "run_id": run_id,
                    "result_status": result_status,
                },
                trace_id=trace_id,
                session_id=None,
                task_id=task_id,
                workspace_id=None,
                idempotency_key=f"skill-run:{run_id}",
            )
            await self._event_pg(
                conn,
                f"skill.{result_status}",
                {"skill_id": skill_id, "version": version, "run_id": run_id},
                trace_id=trace_id,
                session_id=None,
                task_id=task_id,
                workspace_id=None,
                idempotency_key=f"skill-run-status:{run_id}:{result_status}",
            )
            return {
                "run_id": run_id,
                "skill_id": skill_id,
                "version": version,
                "result_status": result_status,
                "duration_ms": duration_ms,
                "cost_usd": cost_usd,
            }

        return await self._pg_tx(tx)

    async def rollback_skill(
        self, skill_id: str, version: int, *, trace_id: str | None = None
    ) -> dict[str, Any]:
        await self._ensure_connected()
        if self.backend == "sqlite":

            def tx(conn: sqlite3.Connection) -> dict[str, Any]:
                row = conn.execute(
                    "SELECT * FROM skill_versions WHERE skill_id=? AND version=?",
                    (skill_id, version),
                ).fetchone()
                if row is None:
                    raise PersonalRuntimeError("NOT_FOUND", f"{skill_id}:{version}")
                now = _now()
                conn.execute(
                    "UPDATE skill_versions SET status='deprecated' WHERE skill_id=? AND status='active'",
                    (skill_id,),
                )
                conn.execute(
                    "UPDATE skill_versions SET status='active',trust_status='trusted',updated_at=? WHERE skill_id=? AND version=?",
                    (now, skill_id, version),
                )
                conn.execute(
                    "UPDATE skill_records SET current_version=?,updated_at=? WHERE id=?",
                    (version, now, skill_id),
                )
                self._event_sqlite(
                    conn,
                    "skill.rolled_back",
                    {"skill_id": skill_id, "version": version},
                    trace_id=trace_id,
                    session_id=None,
                    task_id=None,
                    workspace_id=None,
                    idempotency_key=f"skill-rollback:{skill_id}:{version}",
                )
                return {"status": "rolled_back", "skill_id": skill_id, "version": version}

            return await asyncio.to_thread(self._sqlite_tx, tx)

        async def tx(conn: Any) -> dict[str, Any]:
            row = await conn.fetchrow(
                "SELECT id FROM skill_versions WHERE skill_id=$1 AND version=$2 FOR UPDATE",
                skill_id,
                version,
            )
            if row is None:
                raise PersonalRuntimeError("NOT_FOUND", f"{skill_id}:{version}")
            now = _now()
            await conn.execute(
                "UPDATE skill_versions SET status='deprecated' WHERE skill_id=$1 AND status='active'",
                skill_id,
            )
            await conn.execute(
                "UPDATE skill_versions SET status='active',trust_status='trusted',updated_at=$1 WHERE skill_id=$2 AND version=$3",
                now,
                skill_id,
                version,
            )
            await conn.execute(
                "UPDATE skill_records SET current_version=$1,updated_at=$2 WHERE id=$3",
                version,
                now,
                skill_id,
            )
            await self._event_pg(
                conn,
                "skill.rolled_back",
                {"skill_id": skill_id, "version": version},
                trace_id=trace_id,
                session_id=None,
                task_id=None,
                workspace_id=None,
                idempotency_key=f"skill-rollback:{skill_id}:{version}",
            )
            return {"status": "rolled_back", "skill_id": skill_id, "version": version}

        return await self._pg_tx(tx)

    # ── continuity projection ───────────────────────────────────────
    async def save_continuity(
        self,
        snapshot: dict[str, Any],
        *,
        user_id: str,
        workspace_id: str | None,
        source_event_cursor: str = "",
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_connected()
        sid = _new_id("continuity")
        now = _now()
        payload = dict(snapshot)
        payload.update(
            {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "generated_at": now,
                "source_event_cursor": source_event_cursor,
            }
        )
        if self.backend == "sqlite":

            def tx(conn: sqlite3.Connection) -> dict[str, Any]:
                conn.execute(
                    "INSERT INTO continuity_snapshots(id,user_id,workspace_id,snapshot_json,source_event_cursor,generated_at,schema_version) VALUES(?,?,?,?,?,?,1)",
                    (sid, user_id, workspace_id, _json(payload), source_event_cursor, now),
                )
                self._event_sqlite(
                    conn,
                    "continuity.snapshot_created",
                    {"snapshot_id": sid, "source_event_cursor": source_event_cursor},
                    trace_id=trace_id,
                    session_id=None,
                    task_id=None,
                    workspace_id=workspace_id,
                    idempotency_key=f"continuity:{sid}",
                )
                return {"id": sid, **payload}

            return await asyncio.to_thread(self._sqlite_tx, tx)

        async def tx(conn: Any) -> dict[str, Any]:
            await conn.execute(
                "INSERT INTO continuity_snapshots(id,user_id,workspace_id,snapshot_json,source_event_cursor,generated_at,schema_version) VALUES($1,$2,$3,$4,$5,$6,1)",
                sid,
                user_id,
                workspace_id,
                _json(payload),
                source_event_cursor,
                now,
            )
            await self._event_pg(
                conn,
                "continuity.snapshot_created",
                {"snapshot_id": sid, "source_event_cursor": source_event_cursor},
                trace_id=trace_id,
                session_id=None,
                task_id=None,
                workspace_id=workspace_id,
                idempotency_key=f"continuity:{sid}",
            )
            return {"id": sid, **payload}

        return await self._pg_tx(tx)

    async def latest_continuity(
        self, *, user_id: str, workspace_id: str | None = None
    ) -> dict[str, Any] | None:
        await self._ensure_connected()
        if self.backend == "sqlite":
            row = await asyncio.to_thread(
                lambda: self._read_sqlite(
                    lambda c: c.execute(
                        "SELECT * FROM continuity_snapshots WHERE user_id=? AND (workspace_id=? OR ? IS NULL) ORDER BY generated_at DESC LIMIT 1",
                        (user_id, workspace_id, workspace_id),
                    ).fetchone()
                )
            )
        else:
            row = await self._pool.fetchrow(
                "SELECT * FROM continuity_snapshots WHERE user_id=$1 AND (workspace_id=$2 OR $2 IS NULL) ORDER BY generated_at DESC LIMIT 1",
                user_id,
                workspace_id,
            )
        if row is None:
            return None
        out = _loads(dict(row)["snapshot_json"], {})
        out["id"] = dict(row)["id"]
        return out

    # ── learning ─────────────────────────────────────────────────────
    async def create_learning_candidate(
        self,
        *,
        pattern_id: str,
        scope: str,
        evidence_task_ids: Iterable[str],
        evidence_trajectory_ids: Iterable[str],
        observation: str,
        hypothesis: str,
        confidence: float,
        candidate_type: str,
        proposed_change: dict[str, Any],
        explicit_teaching: bool = False,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        tasks = list(dict.fromkeys(evidence_task_ids))
        trajectories = list(dict.fromkeys(evidence_trajectory_ids))
        if len(set(tasks)) < 3 and not explicit_teaching:
            raise PersonalRuntimeError(
                "LEARNING_THRESHOLD", "at least three independent tasks are required"
            )
        if candidate_type not in {"memory", "skill", "policy_advisory"}:
            raise PersonalRuntimeError("INVALID_CANDIDATE_TYPE", candidate_type)
        await self._ensure_connected()
        rid = _new_id("learning")
        now = _now()
        if self.backend == "sqlite":

            def tx(conn: sqlite3.Connection) -> dict[str, Any]:
                conn.execute(
                    "INSERT INTO learning_records(id,pattern_id,scope,evidence_task_ids,evidence_trajectory_ids,observation,hypothesis,confidence,candidate_type,proposed_change,eval_result,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(pattern_id,scope) DO UPDATE SET evidence_task_ids=excluded.evidence_task_ids,evidence_trajectory_ids=excluded.evidence_trajectory_ids,observation=excluded.observation,hypothesis=excluded.hypothesis,confidence=excluded.confidence,proposed_change=excluded.proposed_change",
                    (
                        rid,
                        pattern_id,
                        scope,
                        _json(tasks),
                        _json(trajectories),
                        observation,
                        hypothesis,
                        max(0, min(1, float(confidence))),
                        candidate_type,
                        _json(proposed_change),
                        _json({"status": "not_evaluated"}),
                        "candidate",
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM learning_records WHERE pattern_id=? AND scope=?",
                    (pattern_id, scope),
                ).fetchone()
                self._event_sqlite(
                    conn,
                    "learning.candidate_created",
                    {
                        "learning_id": row["id"],
                        "pattern_id": pattern_id,
                        "evidence_task_count": len(tasks),
                    },
                    trace_id=trace_id,
                    session_id=None,
                    task_id=tasks[0] if tasks else None,
                    workspace_id=scope if scope.startswith("workspace:") else None,
                    idempotency_key=f"learning-candidate:{pattern_id}:{scope}",
                )
                return self._learning_out(dict(row))

            return await asyncio.to_thread(self._sqlite_tx, tx)

        async def tx(conn: Any) -> dict[str, Any]:
            await conn.execute(
                "INSERT INTO learning_records(id,pattern_id,scope,evidence_task_ids,evidence_trajectory_ids,observation,hypothesis,confidence,candidate_type,proposed_change,eval_result,status,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) ON CONFLICT(pattern_id,scope) DO UPDATE SET evidence_task_ids=excluded.evidence_task_ids,evidence_trajectory_ids=excluded.evidence_trajectory_ids,observation=excluded.observation,hypothesis=excluded.hypothesis,confidence=excluded.confidence,proposed_change=excluded.proposed_change",
                rid,
                pattern_id,
                scope,
                _json(tasks),
                _json(trajectories),
                observation,
                hypothesis,
                max(0, min(1, float(confidence))),
                candidate_type,
                _json(proposed_change),
                _json({"status": "not_evaluated"}),
                "candidate",
                now,
            )
            row = await conn.fetchrow(
                "SELECT * FROM learning_records WHERE pattern_id=$1 AND scope=$2", pattern_id, scope
            )
            assert row is not None
            await self._event_pg(
                conn,
                "learning.candidate_created",
                {
                    "learning_id": row["id"],
                    "pattern_id": pattern_id,
                    "evidence_task_count": len(tasks),
                },
                trace_id=trace_id,
                session_id=None,
                task_id=tasks[0] if tasks else None,
                workspace_id=scope if scope.startswith("workspace:") else None,
                idempotency_key=f"learning-candidate:{pattern_id}:{scope}",
            )
            return self._learning_out(dict(row))

        return await self._pg_tx(tx)

    async def scan_trajectory_candidates(
        self, *, scope: str | None = None, min_tasks: int = 3, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Create advisory candidates from repeated, accepted trajectories.

        This is deterministic evidence collection only.  It never changes a
        memory, prompt, skill, policy, or active runtime behavior.
        """
        from server.events import event_store

        trajectory_events = event_store.read_all(topics={"trajectory.recorded"})
        eval_events = event_store.read_all(topics={"eval.recorded"})
        passed_tasks = {
            str(event.get("task_id"))
            for event in eval_events
            if event.get("task_id") and bool((event.get("payload") or {}).get("passed"))
        }
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in trajectory_events:
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            task_id = str(event.get("task_id") or payload.get("task_id") or "")
            objective = str(payload.get("objective") or "").strip()
            outcome = str(payload.get("outcome") or "")
            if not task_id or not objective or outcome not in {"completed", "success"}:
                continue
            if task_id not in passed_tasks:
                continue
            grouped.setdefault(objective, []).append(event)
        candidates: list[dict[str, Any]] = []
        for objective, events in grouped.items():
            unique = {}
            for event in events:
                task_id = str(event.get("task_id") or (event.get("payload") or {}).get("task_id"))
                unique[task_id] = event
            if len(unique) < max(3, int(min_tasks)):
                continue
            pattern_id = "trajectory:" + hashlib.sha256(objective.encode("utf-8")).hexdigest()[:24]
            candidate_scope = scope or f"workspace:{os.environ.get('VEYA_WORKSPACE', 'default')}"
            candidate = await self.create_learning_candidate(
                pattern_id=pattern_id,
                scope=candidate_scope,
                evidence_task_ids=sorted(unique),
                evidence_trajectory_ids=[
                    str(event.get("event_id"))
                    for event in unique.values()
                    if event.get("event_id")
                ],
                observation=f"Repeated accepted trajectory: {objective}",
                hypothesis="The verified execution pattern may be reusable, pending replay evaluation.",
                confidence=min(1.0, 0.5 + 0.1 * len(unique)),
                candidate_type="policy_advisory",
                proposed_change={"objective": objective, "source": "trajectory_scan"},
            )
            candidates.append(candidate)
            if len(candidates) >= max(1, int(limit)):
                break
        return candidates

    @staticmethod
    def _learning_out(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for key in (
            "evidence_task_ids",
            "evidence_trajectory_ids",
            "proposed_change",
            "eval_result",
        ):
            out[key] = _loads(out.get(key), {} if key.endswith(("change", "result")) else [])
        return out

    async def record_learning_eval(
        self,
        learning_id: str,
        *,
        baseline_ref: str,
        candidate_ref: str,
        result: dict[str, Any],
        passed: bool,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_connected()
        eid = _new_id("learning_eval")
        now = _now()
        if passed:
            delta = result.get("improvement_delta")
            baseline_score = result.get("baseline_score")
            candidate_score = result.get("candidate_score")
            proven_delta = None
            if isinstance(delta, (int, float)):
                proven_delta = float(delta)
            elif isinstance(baseline_score, (int, float)) and isinstance(
                candidate_score, (int, float)
            ):
                proven_delta = float(candidate_score) - float(baseline_score)
            if proven_delta is None or proven_delta <= 0:
                raise PersonalRuntimeError(
                    "LEARNING_GATE",
                    "a passed eval requires numeric improvement_delta > 0 or candidate_score > baseline_score",
                )
        if passed:
            status = "validated"
        else:
            current = await self.get_learning(learning_id)
            status = "degraded" if current and current.get("status") == "applied" else "rejected"
        if self.backend == "sqlite":

            def tx(conn: sqlite3.Connection) -> dict[str, Any]:
                row = conn.execute(
                    "SELECT id FROM learning_records WHERE id=?", (learning_id,)
                ).fetchone()
                if row is None:
                    raise PersonalRuntimeError("NOT_FOUND", learning_id)
                conn.execute(
                    "INSERT INTO learning_evals(id,learning_record_id,baseline_ref,candidate_ref,result_json,passed,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        eid,
                        learning_id,
                        baseline_ref,
                        candidate_ref,
                        _json(result),
                        1 if passed else 0,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE learning_records SET eval_result=?,status=? WHERE id=?",
                    (
                        _json(
                            {
                                "baseline_ref": baseline_ref,
                                "candidate_ref": candidate_ref,
                                "passed": passed,
                                **result,
                            }
                        ),
                        status,
                        learning_id,
                    ),
                )
                self._event_sqlite(
                    conn,
                    "learning.validated" if passed else "learning.rejected",
                    {"learning_id": learning_id, "eval_id": eid, "passed": passed},
                    trace_id=trace_id,
                    session_id=None,
                    task_id=None,
                    workspace_id=None,
                    idempotency_key=f"learning-eval:{eid}",
                )
                return {
                    "eval_id": eid,
                    "learning_id": learning_id,
                    "passed": passed,
                    "status": status,
                }

            return await asyncio.to_thread(self._sqlite_tx, tx)

        async def tx(conn: Any) -> dict[str, Any]:
            row = await conn.fetchrow("SELECT id FROM learning_records WHERE id=$1", learning_id)
            if row is None:
                raise PersonalRuntimeError("NOT_FOUND", learning_id)
            await conn.execute(
                "INSERT INTO learning_evals(id,learning_record_id,baseline_ref,candidate_ref,result_json,passed,created_at) VALUES($1,$2,$3,$4,$5,$6,$7)",
                eid,
                learning_id,
                baseline_ref,
                candidate_ref,
                _json(result),
                1 if passed else 0,
                now,
            )
            await conn.execute(
                "UPDATE learning_records SET eval_result=$1,status=$2 WHERE id=$3",
                _json(
                    {
                        "baseline_ref": baseline_ref,
                        "candidate_ref": candidate_ref,
                        "passed": passed,
                        **result,
                    }
                ),
                status,
                learning_id,
            )
            await self._event_pg(
                conn,
                "learning.validated" if passed else "learning.rejected",
                {"learning_id": learning_id, "eval_id": eid, "passed": passed},
                trace_id=trace_id,
                session_id=None,
                task_id=None,
                workspace_id=None,
                idempotency_key=f"learning-eval:{eid}",
            )
            return {"eval_id": eid, "learning_id": learning_id, "passed": passed, "status": status}

        return await self._pg_tx(tx)

    async def rollback_learning(
        self, learning_id: str, *, reason: str = "regression detected", trace_id: str | None = None
    ) -> dict[str, Any]:
        """Disable an applied learning change while retaining its evidence."""
        await self._ensure_connected()
        now = _now()
        if self.backend == "sqlite":

            def tx(conn: sqlite3.Connection) -> dict[str, Any]:
                row = conn.execute(
                    "SELECT * FROM learning_records WHERE id=?", (learning_id,)
                ).fetchone()
                if row is None:
                    raise PersonalRuntimeError("NOT_FOUND", learning_id)
                if row["status"] not in {"applied", "degraded", "validated"}:
                    raise PersonalRuntimeError("LEARNING_GATE", f"status={row['status']}")
                previous = _loads(row["eval_result"], {})
                previous.update({"rollback_reason": reason, "rolled_back_at": now})
                conn.execute(
                    "UPDATE learning_records SET status='rejected',eval_result=? WHERE id=?",
                    (_json(previous), learning_id),
                )
                self._event_sqlite(
                    conn,
                    "learning.rolled_back",
                    {"learning_id": learning_id, "reason": reason},
                    trace_id=trace_id,
                    session_id=None,
                    task_id=None,
                    workspace_id=None,
                    idempotency_key=f"learning-rollback:{learning_id}",
                )
                return {"status": "rolled_back", "learning_id": learning_id, "reason": reason}

            return await asyncio.to_thread(self._sqlite_tx, tx)

        async def tx(conn: Any) -> dict[str, Any]:
            row = await conn.fetchrow(
                "SELECT * FROM learning_records WHERE id=$1 FOR UPDATE", learning_id
            )
            if row is None:
                raise PersonalRuntimeError("NOT_FOUND", learning_id)
            if row["status"] not in {"applied", "degraded", "validated"}:
                raise PersonalRuntimeError("LEARNING_GATE", f"status={row['status']}")
            previous = _loads(row["eval_result"], {})
            previous.update({"rollback_reason": reason, "rolled_back_at": now})
            await conn.execute(
                "UPDATE learning_records SET status='rejected',eval_result=$1 WHERE id=$2",
                _json(previous),
                learning_id,
            )
            await self._event_pg(
                conn,
                "learning.rolled_back",
                {"learning_id": learning_id, "reason": reason},
                trace_id=trace_id,
                session_id=None,
                task_id=None,
                workspace_id=None,
                idempotency_key=f"learning-rollback:{learning_id}",
            )
            return {"status": "rolled_back", "learning_id": learning_id, "reason": reason}

        return await self._pg_tx(tx)

    async def list_learning(
        self, *, scope: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        limit = max(1, min(int(limit), 100))
        if self.backend == "sqlite":

            def read(c: sqlite3.Connection) -> list[dict[str, Any]]:
                clauses = []
                params = []
                if scope:
                    clauses.append("scope=?")
                    params.append(scope)
                if status:
                    clauses.append("status=?")
                    params.append(status)
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                return [
                    self._learning_out(dict(r))
                    for r in c.execute(
                        "SELECT * FROM learning_records"
                        + where
                        + " ORDER BY created_at DESC LIMIT ?",
                        (*params, limit),
                    ).fetchall()
                ]

            return await asyncio.to_thread(lambda: self._read_sqlite(read))
        clauses = []
        params = []
        idx = 1
        if scope:
            clauses.append(f"scope=${idx}")
            params.append(scope)
            idx += 1
        if status:
            clauses.append(f"status=${idx}")
            params.append(status)
            idx += 1
        params.append(limit)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._pool.fetch(
            "SELECT * FROM learning_records" + where + f" ORDER BY created_at DESC LIMIT ${idx}",
            *params,
        )
        return [self._learning_out(dict(r)) for r in rows]

    async def get_learning(self, learning_id: str) -> dict[str, Any] | None:
        items = await self.list_learning(limit=100)
        return next((x for x in items if x["id"] == learning_id), None)

    async def apply_learning(
        self, learning_id: str, *, actor: str = "user", trace_id: str | None = None
    ) -> dict[str, Any]:
        """Apply only an already validated candidate after an explicit gate."""
        await self._ensure_connected()
        now = _now()
        if self.backend == "sqlite":

            def tx(conn: sqlite3.Connection) -> dict[str, Any]:
                row = conn.execute(
                    "SELECT * FROM learning_records WHERE id=?", (learning_id,)
                ).fetchone()
                if row is None:
                    raise PersonalRuntimeError("NOT_FOUND", learning_id)
                if row["status"] != "validated":
                    raise PersonalRuntimeError("LEARNING_GATE", f"status={row['status']}")
                conn.execute(
                    "UPDATE learning_records SET status='applied',applied_at=? WHERE id=?",
                    (now, learning_id),
                )
                self._event_sqlite(
                    conn,
                    "learning.applied",
                    {"learning_id": learning_id, "actor": actor},
                    trace_id=trace_id,
                    session_id=None,
                    task_id=None,
                    workspace_id=None,
                    idempotency_key=f"learning-applied:{learning_id}",
                )
                return {"status": "applied", "learning_id": learning_id, "applied_at": now}

            return await asyncio.to_thread(self._sqlite_tx, tx)

        async def tx(conn: Any) -> dict[str, Any]:
            row = await conn.fetchrow(
                "SELECT status FROM learning_records WHERE id=$1 FOR UPDATE", learning_id
            )
            if row is None:
                raise PersonalRuntimeError("NOT_FOUND", learning_id)
            if row["status"] != "validated":
                raise PersonalRuntimeError("LEARNING_GATE", f"status={row['status']}")
            await conn.execute(
                "UPDATE learning_records SET status='applied',applied_at=$1 WHERE id=$2",
                now,
                learning_id,
            )
            await self._event_pg(
                conn,
                "learning.applied",
                {"learning_id": learning_id, "actor": actor},
                trace_id=trace_id,
                session_id=None,
                task_id=None,
                workspace_id=None,
                idempotency_key=f"learning-applied:{learning_id}",
            )
            return {"status": "applied", "learning_id": learning_id, "applied_at": now}

        return await self._pg_tx(tx)

    async def personal_metrics(self) -> dict[str, Any]:
        """Durable counters plus truthful eval metrics for the Personal UI."""
        await self._ensure_connected()

        def calculate(
            memory_rows: list[dict[str, Any]],
            candidate_rows: list[dict[str, Any]],
            skill_run_rows: list[dict[str, Any]],
            learning_rows: list[dict[str, Any]],
            event_types: list[str],
        ) -> dict[str, Any]:
            active = [row for row in memory_rows if row.get("status") == "active"]
            fingerprints = [row.get("canonical_fingerprint") for row in active]
            duplicate_groups = len(fingerprints) - len(set(fingerprints))
            provenance = sum(
                bool(
                    _loads(row.get("source_event_ids"), [])
                    or _loads(row.get("source_session_ids"), [])
                    or _loads(row.get("source_task_ids"), [])
                )
                for row in memory_rows
            ) / len(memory_rows) if memory_rows else 1.0
            conflict_candidates = sum(
                bool(_loads(row.get("conflicts_with"), [])) for row in candidate_rows
            )
            runs = len(skill_run_rows)
            successes = sum(row.get("result_status") == "complete" for row in skill_run_rows)
            failures = sum(row.get("result_status") == "failed" for row in skill_run_rows)
            partials = sum(row.get("result_status") == "partial" for row in skill_run_rows)
            evaluated = sum(row.get("status") in {"validated", "rejected", "applied"} for row in learning_rows)
            validated = sum(row.get("status") in {"validated", "applied"} for row in learning_rows)
            deltas = []
            for row in learning_rows:
                result = _loads(row.get("eval_result"), {})
                delta = result.get("improvement_delta")
                if isinstance(delta, (int, float)):
                    deltas.append(float(delta))
            corrections = event_types.count("memory.corrected")
            rollbacks = event_types.count("skill.rolled_back")
            return {
                "memory_precision_inputs": len(active),
                "provenance_coverage": provenance,
                "duplicate_memory_rate": max(0, duplicate_groups) / len(active) if active else 0.0,
                "memory_conflict_rate": conflict_candidates / len(candidate_rows) if candidate_rows else 0.0,
                "memory_correction_success": 1.0 if corrections else None,
                "memory_precision": None,
                "memory_recall_when_needed": None,
                "unnecessary_memory_use": None,
                "stale_memory_use_rate": None,
                "skill_runs": runs,
                "skill_successes": successes,
                "skill_reuse_success_rate": successes / runs if runs else None,
                "skill_failure_rate": failures / runs if runs else None,
                "skill_partial_rate": partials / runs if runs else None,
                "wrong_skill_activation_rate": None,
                "skill_regression_rate": None,
                "rollback_success_rate": 1.0 if rollbacks else None,
                "learning_candidates": sum(row.get("status") == "candidate" for row in learning_rows),
                "learning_validated": validated,
                "validated_candidate_rate": validated / evaluated if evaluated else None,
                "candidate_precision": None,
                "applied_change_regression": None,
                "improvement_delta": sum(deltas) / len(deltas) if deltas else None,
                "single_failure_learning": False,
            }

        if self.backend == "sqlite":

            def read(conn: sqlite3.Connection) -> dict[str, Any]:
                return calculate(
                    [dict(row) for row in conn.execute("SELECT * FROM memory_records")],
                    [dict(row) for row in conn.execute("SELECT * FROM memory_candidates")],
                    [dict(row) for row in conn.execute("SELECT * FROM skill_runs")],
                    [dict(row) for row in conn.execute("SELECT * FROM learning_records")],
                    [str(row[0]) for row in conn.execute("SELECT event_type FROM personal_events")],
                )

            return _attach_gold_benchmark(await asyncio.to_thread(lambda: self._read_sqlite(read)))
        memory_rows = [dict(row) for row in await self._pool.fetch("SELECT * FROM memory_records")]
        candidate_rows = [dict(row) for row in await self._pool.fetch("SELECT * FROM memory_candidates")]
        skill_run_rows = [dict(row) for row in await self._pool.fetch("SELECT * FROM skill_runs")]
        learning_rows = [dict(row) for row in await self._pool.fetch("SELECT * FROM learning_records")]
        event_types = [str(row["event_type"]) for row in await self._pool.fetch("SELECT event_type FROM personal_events")]
        return _attach_gold_benchmark(
            calculate(memory_rows, candidate_rows, skill_run_rows, learning_rows, event_types)
        )

    async def outbox_status(self) -> dict[str, int]:
        await self._ensure_connected()
        if self.backend == "sqlite":
            return await asyncio.to_thread(
                lambda: self._read_sqlite(
                    lambda c: {
                        "pending": int(
                            c.execute(
                                "SELECT COUNT(*) FROM personal_outbox WHERE published_at IS NULL"
                            ).fetchone()[0]
                        ),
                        "events": int(
                            c.execute("SELECT COUNT(*) FROM personal_events").fetchone()[0]
                        ),
                    }
                )
            )
        row = await self._pool.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE published_at IS NULL) AS pending, COUNT(*) AS events FROM personal_outbox po LEFT JOIN personal_events pe ON pe.id=po.event_id"
        )
        return {"pending": int(row["pending"]), "events": int(row["events"])}

    async def publish_outbox(self, *, limit: int = 100) -> dict[str, int]:
        """Project committed personal events into the existing SSE EventStore.

        The database remains authoritative. EventStore de-duplicates by the
        durable event id, so a crash between publish and marking the outbox row
        is safe and replayable.
        """
        await self._ensure_connected()
        limit = max(1, min(int(limit), 500))
        if self.backend == "sqlite":
            rows = await asyncio.to_thread(
                lambda: self._read_sqlite(
                    lambda c: c.execute(
                        "SELECT po.event_id,pe.* FROM personal_outbox po JOIN personal_events pe ON pe.id=po.event_id WHERE po.published_at IS NULL AND po.next_attempt_at<=? ORDER BY pe.occurred_at LIMIT ?",
                        (_now(), limit),
                    ).fetchall()
                )
            )
        else:
            rows = await self._pool.fetch(
                "SELECT po.event_id,pe.* FROM personal_outbox po JOIN personal_events pe ON pe.id=po.event_id WHERE po.published_at IS NULL AND po.next_attempt_at<=EXTRACT(EPOCH FROM clock_timestamp()) ORDER BY pe.occurred_at LIMIT $1",
                limit,
            )
        if not rows:
            return {"published": 0, "failed": 0, "pending": 0}
        from server.events import event_store

        published = failed = 0
        for raw in rows:
            event = dict(raw)
            event["payload"] = _loads(event.pop("payload_json", "{}"), {})
            projected = {
                "event_id": event["id"],
                "topic": event["event_type"],
                "session_id": event.get("session_id") or "unknown",
                "trace_id": event.get("trace_id") or event.get("id"),
                "task_id": event.get("task_id"),
                "actor": "personal-runtime",
                "ts": event.get("occurred_at"),
                "payload": event["payload"],
            }
            try:
                await asyncio.to_thread(event_store.append, projected)
                if self.backend == "sqlite":
                    await asyncio.to_thread(
                        self._sqlite_tx,
                        lambda c, eid=event["id"]: c.execute(
                            "UPDATE personal_outbox SET published_at=? WHERE event_id=? AND published_at IS NULL",
                            (_now(), eid),
                        ).rowcount,
                    )
                else:
                    await self._pg_tx(
                        lambda c, eid=event["id"]: c.execute(
                            "UPDATE personal_outbox SET published_at=$1 WHERE event_id=$2 AND published_at IS NULL",
                            _now(),
                            eid,
                        )
                    )
                published += 1
            except Exception as exc:
                failed += 1
                if self.backend == "sqlite":
                    await asyncio.to_thread(
                        self._sqlite_tx,
                        lambda c, eid=event["id"], msg=str(exc)[:500]: c.execute(
                            "UPDATE personal_outbox SET publish_attempts=publish_attempts+1,last_error=?,next_attempt_at=? WHERE event_id=?",
                            (msg, _now() + 1, eid),
                        ),
                    )
                else:
                    await self._pg_tx(
                        lambda c, eid=event["id"], msg=str(exc)[:500]: c.execute(
                            "UPDATE personal_outbox SET publish_attempts=publish_attempts+1,last_error=$1,next_attempt_at=$2 WHERE event_id=$3",
                            msg,
                            _now() + 1,
                            eid,
                        )
                    )
        status = await self.outbox_status()
        return {"published": published, "failed": failed, "pending": status["pending"]}

    async def health(self) -> dict[str, Any]:
        try:
            await self._ensure_connected()
            outbox = await self.outbox_status()
            now = _now()
            if self.backend == "sqlite":
                meta = await asyncio.to_thread(
                    lambda: self._read_sqlite(
                        lambda c: c.execute(
                            "SELECT COALESCE(MAX(version),0) FROM execution_schema_meta"
                        ).fetchone()[0]
                    )
                )
                counts = await asyncio.to_thread(
                    lambda: self._read_sqlite(
                        lambda c: {
                            "memory": int(
                                c.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0]
                            ),
                            "skills": int(
                                c.execute("SELECT COUNT(*) FROM skill_records").fetchone()[0]
                            ),
                            "learning": int(
                                c.execute("SELECT COUNT(*) FROM learning_records").fetchone()[0]
                            ),
                        }
                    )
                )
                execution = await asyncio.to_thread(
                    lambda: self._read_sqlite(
                        lambda c: {
                            "queue_depth": int(
                                c.execute(
                                    "SELECT COUNT(*) FROM work_items WHERE state IN ('ready','retry_wait')"
                                ).fetchone()[0]
                            ),
                            "active_leases": int(
                                c.execute(
                                    "SELECT COUNT(*) FROM execution_leases WHERE released_at IS NULL AND expires_at>?",
                                    (now,),
                                ).fetchone()[0]
                            ),
                            "expired_leases": int(
                                c.execute(
                                    "SELECT COUNT(*) FROM execution_leases WHERE released_at IS NULL AND expires_at<=?",
                                    (now,),
                                ).fetchone()[0]
                            ),
                            "execution_outbox_pending": int(
                                c.execute(
                                    "SELECT COUNT(*) FROM execution_outbox WHERE published_at IS NULL"
                                ).fetchone()[0]
                            ),
                            "quarantined_count": int(
                                c.execute(
                                    "SELECT COUNT(*) FROM work_items WHERE state='quarantined_unknown'"
                                ).fetchone()[0]
                                + c.execute(
                                    "SELECT COUNT(*) FROM side_effects WHERE state='manual_review'"
                                ).fetchone()[0]
                            ),
                            "reconciler_last_success": (
                                lambda row: row[0] if row else None
                            )(
                                c.execute(
                                    "SELECT occurred_at FROM execution_events WHERE event_type IN ('recovery.completed','reconciliation.completed') ORDER BY occurred_at DESC LIMIT 1"
                                ).fetchone()
                            ),
                        }
                    )
                )
            else:
                meta = await self._pool.fetchval(
                    "SELECT COALESCE(MAX(version),0) FROM execution_schema_meta"
                )
                counts = {
                    "memory": int(await self._pool.fetchval("SELECT COUNT(*) FROM memory_records")),
                    "skills": int(await self._pool.fetchval("SELECT COUNT(*) FROM skill_records")),
                    "learning": int(
                        await self._pool.fetchval("SELECT COUNT(*) FROM learning_records")
                    ),
                }
                row = await self._pool.fetchrow(
                    "SELECT "
                    "(SELECT COUNT(*) FROM work_items WHERE state IN ('ready','retry_wait')) AS queue_depth, "
                    "(SELECT COUNT(*) FROM execution_leases WHERE released_at IS NULL AND expires_at>$1) AS active_leases, "
                    "(SELECT COUNT(*) FROM execution_leases WHERE released_at IS NULL AND expires_at<=$1) AS expired_leases, "
                    "(SELECT COUNT(*) FROM execution_outbox WHERE published_at IS NULL) AS execution_outbox_pending, "
                    "(SELECT COUNT(*) FROM work_items WHERE state='quarantined_unknown') + "
                    "(SELECT COUNT(*) FROM side_effects WHERE state='manual_review') AS quarantined_count, "
                    "(SELECT occurred_at FROM execution_events WHERE event_type IN ('recovery.completed','reconciliation.completed') ORDER BY occurred_at DESC LIMIT 1) AS reconciler_last_success",
                    now,
                )
                execution = dict(row)
            reconciler_last = execution.get("reconciler_last_success")
            if hasattr(reconciler_last, "timestamp"):
                reconciler_last = reconciler_last.timestamp()
            return {
                "enabled": True,
                "backend": "postgres" if self.backend == "postgres" else "sqlite",
                "authority": "postgresql" if self.backend == "postgres" else "sqlite_local",
                "healthy": True,
                "schema_version": int(meta or 0),
                "pending_outbox": outbox["pending"],
                "queue_depth": int(execution["queue_depth"]),
                "active_leases": int(execution["active_leases"]),
                "expired_leases": int(execution["expired_leases"]),
                "execution_outbox_pending": int(execution["execution_outbox_pending"]),
                "quarantined_count": int(execution["quarantined_count"]),
                "reconciler_status": {
                    "enabled": _flag("VEYA_EXECUTION_RECONCILER"),
                    "status": "ok" if reconciler_last is not None else "not_observed",
                    "last_success_at": reconciler_last,
                },
                "metrics": await self.personal_metrics(),
                "counts": counts,
                "feature_flags": {name: _flag(name) for name in _FEATURE_FLAGS},
            }
        except Exception as exc:
            return {
                "enabled": True,
                "backend": "postgres" if self.backend == "postgres" else "sqlite",
                "authority": "postgresql" if self.backend == "postgres" else "sqlite_local",
                "healthy": False,
                "error": f"{type(exc).__name__}: {exc}",
            }


_PERSONAL_RUNTIME: PersonalRuntimeStore | None = None


def get_personal_runtime() -> PersonalRuntimeStore:
    global _PERSONAL_RUNTIME
    if _PERSONAL_RUNTIME is None:
        _PERSONAL_RUNTIME = PersonalRuntimeStore(
            dsn=os.environ.get("VEYA_EXECUTION_DATABASE_URL") or os.environ.get("DATABASE_URL"),
            sqlite_path=os.environ.get(
                "VEYA_EXECUTION_SQLITE_PATH", ".veya/execution-runtime.sqlite3"
            ),
            production=os.environ.get("VEYA_EXECUTION_PRODUCTION", "0").strip().lower()
            not in {"", "0", "false", "off", "no"},
        )
    return _PERSONAL_RUNTIME


def reset_personal_runtime() -> None:
    global _PERSONAL_RUNTIME
    _PERSONAL_RUNTIME = None
