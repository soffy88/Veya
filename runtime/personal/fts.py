"""Postgres full-text search helpers for durable memory records.

Design adapted from block/buzz (Apache-2.0, Copyright 2026 Block, Inc.):
generated tsvector + GIN candidates, tenant-scoped query, hard limits, and
candidate ids re-fetched and re-authorized by the caller.  CJK queries have no
'simple' tsvector tokens and fall back to the existing ILIKE path untouched.
"""

from __future__ import annotations

import os
import re

MAX_QUERY_CHARS = 4096
MAX_RESULTS = 100
MAX_PAGE = 1000
_ASCII_WORD = re.compile(r"[a-z0-9]+")

__all__ = [
    "MAX_PAGE",
    "MAX_QUERY_CHARS",
    "MAX_RESULTS",
    "fts_enabled",
    "normalize_query",
    "search_candidate_ids_sql",
]


def fts_enabled() -> bool:
    """``VEYA_FTS`` gates Postgres FTS; default ON (SQLite is unaffected)."""

    return os.environ.get("VEYA_FTS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
        "",
    }


def normalize_query(query: str) -> str:
    """Bound query length and normalize whitespace."""

    return " ".join(str(query or "").split())[:MAX_QUERY_CHARS]


def has_latin_tokens(query: str) -> bool:
    return bool(_ASCII_WORD.search(query.lower()))


def search_candidate_ids_sql(clauses: list[str], params: list[object]) -> tuple[str, list[object]]:
    """Candidate-id query only; no content is trusted from this query.

    The generated column and GIN index are installed by the additive migration
    in ``runtime.execution.schema.POSTGRES_ADDITIVE_DDL``. Callers must re-fetch
    every returned id and re-apply TenantContext/user authorization before
    exposing content.
    """

    if not params:
        raise ValueError("search parameters must be built by the caller")
    query_idx = len(params) + 1
    params.append(normalize_query(str(params[-1])))
    # websearch_to_tsquery with 'simple' never interprets user input as SQL;
    # candidate row visibility is deliberately not decided here.
    sql = (
        "SELECT id FROM memory_records WHERE "
        + " AND ".join(clauses)
        + f" AND search_tsv @@ websearch_to_tsquery('simple', ${query_idx})"
        + " ORDER BY confidence DESC, updated_at DESC"
    )
    return sql, params
