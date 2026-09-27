"""The durable execution store must migrate a pre-P3-A schema.

Regression: ``declare_side_effect`` writes ``request_fingerprint``, but the
column was added to the schema without a matching ``ALTER TABLE`` migration.
Any database created before that change fails its first side-effect write with
``table side_effects has no column named request_fingerprint``, which blocks
every governed physical step — including the qualified L0/L2 execution path.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from runtime.execution.durable import DurableExecutionRepository


def _stale_db(path: Path) -> None:
    """A side_effects table with the pre-P3-A column set."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE side_effects ("
        "id TEXT PRIMARY KEY, goal_run_id TEXT, work_item_id TEXT, "
        "operation_key TEXT UNIQUE, operation_type TEXT, target_ref TEXT, "
        "state TEXT, request_hash TEXT, provider_request_id TEXT, "
        "probe_policy TEXT, probe_result_json TEXT, compensation_json TEXT, "
        "first_seen_at REAL, last_seen_at REAL, revision INTEGER, "
        "bot_id TEXT NOT NULL DEFAULT 'veya-default')"
    )
    conn.execute("CREATE TABLE execution_schema_meta (version INTEGER, applied_at REAL)")
    conn.execute("INSERT INTO execution_schema_meta VALUES(0, 0.0)")
    conn.commit()
    conn.close()


async def _connect(path: Path) -> DurableExecutionRepository:
    repository = DurableExecutionRepository(sqlite_path=str(path))
    await repository.connect()
    return repository


def _columns(path: Path, table: str = "side_effects") -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_migration_adds_request_fingerprint_to_stale_schema(tmp_path: Path) -> None:
    db = tmp_path / "stale.sqlite3"
    _stale_db(db)
    assert "request_fingerprint" not in _columns(db)

    asyncio.run(_connect(db))

    assert "request_fingerprint" in _columns(db)


def test_migration_preserves_existing_rows(tmp_path: Path) -> None:
    """A migration must not destroy recorded side effects."""
    db = tmp_path / "stale.sqlite3"
    _stale_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO side_effects(id,goal_run_id,work_item_id,operation_key,"
        "operation_type,target_ref,state,request_hash,first_seen_at,last_seen_at,revision,bot_id)"
        " VALUES('1','g','w','op-1','write','a.txt','committed','h1',1.0,1.0,1,'veya-default')"
    )
    conn.commit()
    conn.close()

    asyncio.run(_connect(db))

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT operation_key, state FROM side_effects").fetchall()
    finally:
        conn.close()
    assert rows == [("op-1", "committed")]


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Connecting repeatedly must not fail or duplicate the column."""
    db = tmp_path / "stale.sqlite3"
    _stale_db(db)
    asyncio.run(_connect(db))
    first = _columns(db)
    asyncio.run(_connect(db))
    assert _columns(db) == first
    assert "request_fingerprint" in first


def test_side_effect_write_succeeds_after_migration(tmp_path: Path) -> None:
    """The end-to-end symptom: a side effect can be declared at all."""
    db = tmp_path / "stale.sqlite3"
    _stale_db(db)

    async def scenario() -> None:
        repository = await _connect(db)
        await repository.declare_side_effect(
            goal_run_id="goal-1",
            work_item_id="task-1",
            operation_key="op-1",
            operation_type="write",
            target_ref="probe.txt",
            request={"tool": "write_file"},
            request_fingerprint="fp-1",
        )

    asyncio.run(scenario())
    assert "request_fingerprint" in _columns(db)


def test_bot_id_migration_still_present(tmp_path: Path) -> None:
    """The pre-existing bot_id backfill must not regress."""
    db = tmp_path / "bare.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE side_effects ("
        "id TEXT PRIMARY KEY, goal_run_id TEXT, work_item_id TEXT, "
        "operation_key TEXT UNIQUE, operation_type TEXT, target_ref TEXT, "
        "state TEXT, request_hash TEXT, provider_request_id TEXT, "
        "probe_policy TEXT, probe_result_json TEXT, compensation_json TEXT, "
        "first_seen_at REAL, last_seen_at REAL, revision INTEGER)"
    )
    conn.execute("CREATE TABLE execution_schema_meta (version INTEGER, applied_at REAL)")
    conn.execute("INSERT INTO execution_schema_meta VALUES(0, 0.0)")
    conn.commit()
    conn.close()

    asyncio.run(_connect(db))

    columns = _columns(db)
    assert "bot_id" in columns
    assert "request_fingerprint" in columns


@pytest.mark.parametrize("table", ["side_effects", "execution_schema_meta"])
def test_migration_leaves_meta_intact(tmp_path: Path, table: str) -> None:
    db = tmp_path / "stale.sqlite3"
    _stale_db(db)
    asyncio.run(_connect(db))
    assert _columns(db, table), f"{table} must survive migration"
