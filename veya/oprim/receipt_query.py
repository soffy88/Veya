"""veya/oprim/receipt_query — one atomic per-source query against one store.

A *receipt* is the smallest citable unit "have we seen this before?" can return:

```json
{
  "receipt_id": "session:abc:3",
  "source": "session" | "goal_run" | "group_message" | "approval" | "annotation",
  "ref": "abc",
  "title": "fix flaky login test",
  "text": "… matched body / snippet …",
  "uri": "/sessions/abc?revision=3",
  "ts": 1750000000.0
}
```

Exactly one source per call (§4.1 atomicity: one oprim = one external store).  The omodul
that merges results therefore never learns any store's schema — it only knows the
receipt shape above.  Sources are supplied by layer4 as injected callables
(``inject(query, source, *, limit, since) -> list[receipt]``); this module owns the
receipt *shape*, the deterministic ``limit``/``since`` normalisation, and the citation
URI builder.

Single-source (§1.4): ``bm25_search`` (oprim) is the canonical keyword ranker and is
reused for scoring rather than reimplemented.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "RECEIPT_SOURCES",
    "RECEIPT_FIELDS",
    "normalize_receipt",
    "normalize_query",
    "receipt_uri",
]

#: Every source the unified search may draw from.  ``sessions`` is backed by the
#: existing W2 FTS store; the rest are layer4 projections over existing stores.
RECEIPT_SOURCES: tuple[str, ...] = (
    "session",
    "goal_run",
    "group_message",
    "approval",
    "annotation",
)

#: Canonical receipt keys.  Anything else in a source row is dropped so the
#: merge step cannot accidentally depend on a store-private field.
RECEIPT_FIELDS: tuple[str, ...] = (
    "receipt_id",
    "source",
    "ref",
    "title",
    "text",
    "uri",
    "ts",
)


def receipt_uri(source: str, ref: str) -> str:
    """Build the deep link a receipt cites.

    Args:
        source: one of :data:`RECEIPT_SOURCES`.
        ref: the store-native identifier.

    Returns:
        A relative URI an operator can open, or ``""`` when the pair is unknown.

    Example:
        >>> receipt_uri("session", "s1")
        '/sessions/s1'
    """
    if not isinstance(ref, str) or not ref:
        return ""
    if source == "session":
        return f"/sessions/{ref}"
    if source == "goal_run":
        return f"/goals/{ref}"
    if source == "group_message":
        return f"/bots/groups/{ref}/messages"
    if source == "approval":
        return f"/governance/approvals/{ref}"
    if source == "annotation":
        return f"/review/annotations/{ref}"
    return ""


def normalize_receipt(
    source: str,
    row: dict[str, Any],
    *,
    ref: str = "",
    title: str = "",
    text: str = "",
    uri: str = "",
    ts: float = 0.0,
) -> dict[str, Any]:
    """Project one store row onto the canonical receipt shape.

    Source rows win over the keyword arguments only when they actually carry a
    value, so a source that already knows its title does not need to be told.

    Args:
        source: the source name (must be in :data:`RECEIPT_SOURCES`).
        row: the raw store record; read-only.
        ref / title / text / uri / ts: fallbacks used when ``row`` lacks them.

    Returns:
        A dict with exactly :data:`RECEIPT_FIELDS`, sorted-key-free, values coerced.

    Example:
        >>> normalize_receipt("session", {"sid": "s1", "snippet": "hi"}, ts=5.0)["uri"]
        '/sessions/s1'
    """
    data = row if isinstance(row, dict) else {}
    resolved_ref = str(data.get("ref") or data.get("id") or data.get("sid") or ref or "")
    resolved_title = str(data.get("title") or title or "")
    resolved_text = str(data.get("text") or data.get("snippet") or data.get("body") or text or "")
    resolved_uri = str(data.get("uri") or uri or receipt_uri(source, resolved_ref))
    raw_ts = data.get("ts", ts)
    try:
        resolved_ts = float(raw_ts)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        resolved_ts = 0.0
    return {
        "receipt_id": f"{source}:{resolved_ref or resolved_title}",
        "source": source,
        "ref": resolved_ref,
        "title": resolved_title,
        "text": resolved_text,
        "uri": resolved_uri,
        "ts": resolved_ts,
    }


def normalize_query(query: str, *, limit: int = 20, since: float = 0.0) -> dict[str, Any]:
    """Normalise one per-source query so every source is asked identically.

    Args:
        query: the raw user query.
        limit: per-source result cap, clamped to ``1..100``.
        since: unix timestamp lower bound; ``0`` means "no bound".

    Returns:
        ``{"query": str, "terms": [...], "limit": int, "since": float}``.
        ``terms`` is the deterministic lowercased token list used for scoring.

    Example:
        >>> normalize_query("Fix The Flaky Test")["terms"]
        ['fix', 'the', 'flaky', 'test']
    """
    text = query if isinstance(query, str) else ""
    tokens = [t for t in "".join(c if c.isalnum() else " " for c in text.lower()).split() if t]
    try:
        capped = int(limit)
    except (TypeError, ValueError):
        capped = 20
    capped = max(1, min(100, capped))
    try:
        lower = float(since)
    except (TypeError, ValueError):
        lower = 0.0
    return {
        "query": text.strip(),
        "terms": tokens,
        "limit": capped,
        "since": max(0.0, lower),
    }