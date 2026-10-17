"""Regression tests for asyncpg Record-compatible durable metrics rows."""

from __future__ import annotations

from typing import Any

import pytest

from runtime.execution.durable import DurableExecutionRepository


class _Record:
    """Minimal asyncpg.Record lookalike: mapping access, no dict isinstance."""

    def __init__(self, **values: Any) -> None:
        self._values = values

    def __getitem__(self, key: str) -> Any:
        return self._values[key]


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetch(self, query: str) -> list[Any]:
        self.queries.append(query)
        if "FROM execution_events" in query:
            return [
                _Record(event_type="work_item.created", count=3),
                _Record(event_type="work_item.claimed", count=2),
            ]
        if "FROM recovery_decisions" in query:
            return [
                _Record(decision="RETRY_SAFE", count=2),
                _Record(decision="COMPLETED_FROM_EVIDENCE", count=1),
            ]
        if "FROM work_items wi" in query:
            return []
        if "FROM work_attempts wa" in query:
            return []
        raise AssertionError(f"unexpected query: {query}")

    async def fetchval(self, query: str) -> int:
        self.queries.append(query)
        return 0


class _Acquire:
    def __init__(self, conn: _Connection) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Connection:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, conn: _Connection) -> None:
        self.conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_metrics_accept_asyncpg_record_rows() -> None:
    repo = DurableExecutionRepository(dsn="postgresql://unused", production=True)
    repo._pool = _Pool(_Connection())

    metrics = await repo.metrics()

    assert metrics["jobs_enqueued"] == 3
    assert metrics["jobs_claimed"] == 2
    assert metrics["lease_reclaimed"] == 2
    assert metrics["dangling_detected"] == 3
    assert metrics["dangling_recovered"] == 3
