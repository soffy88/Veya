from __future__ import annotations

import asyncio

import pytest

from runtime.execution.durable import DurableExecutionError
from runtime.execution.runtime import DurableExecutionRuntime, DurableRuntimeConfig
from runtime.execution.schema import SCHEMA_VERSION


@pytest.mark.asyncio
async def test_runtime_start_reconciles_before_ready(tmp_path):
    runtime = DurableExecutionRuntime(
        DurableRuntimeConfig(
            enabled=True,
            sqlite_path=str(tmp_path / "runtime.sqlite3"),
            queue_read=True,
            queue_claim=True,
            reconciler_enabled=False,
            finalization_resume=True,
            event_outbox=True,
        )
    )
    try:
        health = await runtime.start()
        assert health["ok"] is True
        assert health["backend"] == "sqlite"
        assert health["schema_version"] == SCHEMA_VERSION
        assert health["started"] is True
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_start_serializes_concurrent_migration_entrypoints(tmp_path, monkeypatch):
    runtime = DurableExecutionRuntime(
        DurableRuntimeConfig(
            enabled=True,
            sqlite_path=str(tmp_path / "runtime.sqlite3"),
            queue_read=True,
            queue_claim=True,
            reconciler_enabled=False,
            finalization_resume=True,
            event_outbox=True,
        )
    )
    connect_calls = 0

    async def connect_once():
        nonlocal connect_calls
        connect_calls += 1
        await asyncio.sleep(0.01)

    async def health():
        return {"ok": True, "backend": "test", "schema_version": SCHEMA_VERSION}

    monkeypatch.setattr(runtime.repository, "connect", connect_once)
    monkeypatch.setattr(runtime.repository, "health", health)
    monkeypatch.setattr(runtime.repository, "metrics", lambda: asyncio.sleep(0, result={}))
    monkeypatch.setattr(runtime.reconciler, "startup", lambda: asyncio.sleep(0))
    try:
        results = await asyncio.gather(runtime.start(), runtime.start())
        assert connect_calls == 1
        assert all(result["started"] is True for result in results)
    finally:
        await runtime.close()


def test_disabled_runtime_does_not_construct_a_production_sqlite_authority(monkeypatch):
    monkeypatch.setenv("VEYA_EXECUTION_PRODUCTION", "1")
    runtime = DurableExecutionRuntime(DurableRuntimeConfig(enabled=False, event_outbox=False))
    assert runtime.config.enabled is False


def test_runtime_rejects_invalid_lease_or_production_sqlite():
    with pytest.raises(DurableExecutionError, match="2x"):
        DurableRuntimeConfig(
            enabled=True, event_outbox=True, lease_ttl_s=10, heartbeat_interval_s=6
        ).validate()
    with pytest.raises(DurableExecutionError, match="PostgreSQL DSN"):
        DurableRuntimeConfig(enabled=True, production=True, event_outbox=True).validate()


def test_runtime_rejects_explicit_sqlite_in_production():
    with pytest.raises(DurableExecutionError, match="SQLite is local/test only"):
        DurableRuntimeConfig(
            enabled=True,
            production=True,
            database_url="sqlite:///tmp/runtime.sqlite3",
            event_outbox=True,
        ).validate()
