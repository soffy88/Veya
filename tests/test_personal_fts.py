"""Postgres FTS design contract: additive DDL, tenant scope, hard caps."""

from __future__ import annotations

import pytest

from runtime.execution.schema import POSTGRES_ADDITIVE_DDL
from runtime.personal import fts
from runtime.personal.runtime import PersonalRuntimeStore


def test_additive_ddl_has_generated_tsvector_and_gin():
    ddl = "\n".join(POSTGRES_ADDITIVE_DDL).lower()
    assert "add column if not exists search_tsv tsvector" in ddl
    assert "generated always as" in ddl
    assert "to_tsvector('simple'" in ddl
    assert "using gin (search_tsv)" in ddl
    assert "alter table memory_records" in ddl
    assert "add column if not exists tenant_id" in ddl
    assert "memory_tenant_idx" in ddl


def test_flag_default_on_and_query_limits():
    from unittest.mock import patch

    with patch.dict("os.environ", {}, clear=True):
        assert fts.fts_enabled() is True
    with patch.dict("os.environ", {"VEYA_FTS": "0"}):
        assert fts.fts_enabled() is False
    assert fts.normalize_query("  " + "ab" * 5000 + "  ") == "ab" * 2048
    assert fts.MAX_RESULTS == 100


def test_cjk_queries_use_existing_path():
    assert fts.has_latin_tokens("python search") is True
    assert fts.has_latin_tokens("中文搜索") is False


def test_candidate_sql_is_tenant_scoped_and_returns_ids_only():
    clauses = ["status = ANY($1::text[])", "scope_type=$2"]
    params: list[object] = [["active"], "user"]
    sql, new_params = fts.search_candidate_ids_sql(clauses, params)
    assert sql.startswith("SELECT id FROM memory_records WHERE")
    assert "search_tsv @@ websearch_to_tsquery('simple', $3)" in sql
    assert len(new_params) == 3


@pytest.mark.asyncio
async def test_sqlite_search_unchanged_and_bounded(tmp_path):
    store = PersonalRuntimeStore(sqlite_path=tmp_path / "p.db", production=False)
    await store.connect()
    try:
        candidate = await store.create_memory_candidate(
            "alpha beta",
            scope_type="user",
            scope_id="u1",
            memory_type="semantic",
            reason="fts test",
        )
        committed = await store.commit_memory_candidate(candidate["id"])
        mid = committed["record"]["id"]
        assert [m["id"] for m in await store.search_memory("alpha", scope_type="user", scope_id="u1")] == [mid]
        assert await store.search_memory("alpha", scope_type="user", scope_id="someone-else") == []
        # Oversized limit is clamped, never passed through unbounded.
        assert len(await store.search_memory("alpha", limit=100000)) <= fts.MAX_RESULTS
    finally:
        await store.close()
