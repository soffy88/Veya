"""3O-PURE — receipt_rank_merge: rank and merge search receipts with citations.

The deterministic half of NANO-W9 "search with receipts": given receipts from
several sources, produce a single ordered answer whose every line cites a source.

Scoring (all three terms are pure arithmetic on the receipt dict):

* ``text``      — fraction of query terms present in ``title + text``
* ``recency``   — exponential decay, ``0.5 ** (age_s / half_life_s)``
* ``source``    — a per-source prior (an approval record is worth more than a
  passing chat message when answering "did we already decide this?")

Total order is ``(-score, receipt_id)`` so the same input always yields the same
output — the property that makes the answer reproducible and testable.

This is deliberately **not** ``oprim.bm25_search``: BM25 ranks a query against a
document corpus with IDF, this ranks a handful of heterogeneous receipts with a
recency decay and a source prior. Different function, no duplicated logic.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "SOURCE_PRIOR",
    "build_citation",
    "build_answer",
    "merge_receipts",
    "rank_receipts",
    "score_receipt",
]

#: Source priors.  An approval is a decision of record; a chat line is hearsay.
SOURCE_PRIOR: dict[str, float] = {
    "approval": 1.0,
    "goal_run": 0.9,
    "annotation": 0.8,
    "session": 0.7,
    "group_message": 0.5,
}

_DEFAULT_WEIGHTS: dict[str, float] = {"text": 1.0, "recency": 0.5, "source": 0.5}
_DEFAULT_HALF_LIFE_S = 7 * 24 * 3600.0


def _terms(value: Any) -> list[str]:
    text = value if isinstance(value, str) else ""
    return [t for t in "".join(c if c.isalnum() else " " for c in text.lower()).split() if t]


def score_receipt(
    receipt: dict[str, Any],
    *,
    terms: list[str],
    now: float = 0.0,
    half_life_s: float = _DEFAULT_HALF_LIFE_S,
    weights: dict[str, float] | None = None,
) -> float:
    """Score one receipt against the query terms.

    Args:
        receipt: a canonical receipt dict (``title``/``text``/``source``/``ts``).
        terms: pre-tokenised query terms.
        now: reference unix timestamp for the recency term; ``0`` disables decay
            (deterministic tests pass ``0``).
        half_life_s: recency half-life in seconds.
        weights: optional override of the three term weights.

    Returns:
        A non-negative float; ``0.0`` when no term matches and the receipt is
        old/unprioritised.

    Example:
        >>> round(score_receipt({"title": "fix login", "source": "session", "ts": 0},
        ...                     terms=["login"]), 4)
        1.2
    """
    if not isinstance(receipt, dict):
        return 0.0
    w = dict(_DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)

    haystack = _terms(receipt.get("title")) + _terms(receipt.get("text"))
    if terms and haystack:
        bag = set(haystack)
        hit = sum(1 for t in terms if t in bag)
        text_score = hit / len(terms)
    else:
        text_score = 0.0

    recency_score = 0.0
    ts = receipt.get("ts")
    if now and isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        age = max(0.0, float(now) - float(ts))
        life = half_life_s if half_life_s and half_life_s > 0 else _DEFAULT_HALF_LIFE_S
        recency_score = 0.5 ** (age / life)

    source_score = SOURCE_PRIOR.get(str(receipt.get("source") or ""), 0.4)

    return (
        w["text"] * text_score + w["recency"] * recency_score + w["source"] * source_score
    )


def rank_receipts(
    receipts: list[dict[str, Any]],
    *,
    terms: list[str],
    now: float = 0.0,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Order receipts best-first, dropping anything that scores zero.

    Args:
        receipts: canonical receipts from any number of sources.
        terms: pre-tokenised query terms.
        now: reference timestamp for recency (``0`` = no decay).
        limit: maximum returned entries.

    Returns:
        New receipt dicts, each with an added ``score`` key, sorted by
        ``(-score, receipt_id)``.  Input dicts are never mutated.

    Example:
        >>> out = rank_receipts([{"receipt_id": "a", "title": "x", "source": "approval"}],
        ...                     terms=["x"])
        >>> out[0]["source"]
        'approval'
    """
    scored: list[dict[str, Any]] = []
    for receipt in receipts or []:
        if not isinstance(receipt, dict):
            continue
        value = score_receipt(receipt, terms=terms, now=now)
        if value <= 0.0:
            continue
        merged = dict(receipt)
        merged["score"] = round(value, 6)
        merged["citation"] = build_citation(receipt)
        scored.append(merged)
    scored.sort(key=lambda r: (-r["score"], str(r.get("receipt_id") or "")))
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 20
    return scored[: max(0, cap)]


def merge_receipts(
    receipts: list[dict[str, Any]],
    *,
    terms: list[str],
    now: float = 0.0,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """De-duplicate by ``receipt_id`` and keep the highest-scoring copy.

    Two sources can legitimately cite the same artefact (a GoalRun result and
    the session turn that produced it).  The merge keeps one receipt and records
    every source it was seen through, so the answer never double-counts.

    Args:
        receipts: canonical receipts, possibly with duplicates.
        terms: pre-tokenised query terms.
        now: reference timestamp for recency.
        limit: maximum returned entries.

    Returns:
        De-duplicated, ranked receipts (see :func:`rank_receipts`).

    Example:
        >>> len(merge_receipts([{"receipt_id": "a", "title": "x", "source": "session"},
        ...                     {"receipt_id": "a", "title": "x", "source": "approval"}],
        ...                    terms=["x"]))
        1
    """
    best: dict[str, dict[str, Any]] = {}
    for receipt in receipts or []:
        if not isinstance(receipt, dict):
            continue
        key = str(receipt.get("receipt_id") or "")
        if not key:
            continue
        value = score_receipt(receipt, terms=terms, now=now)
        current = best.get(key)
        if current is None or value > current["_score"]:
            if current is not None:
                seen = list(current.get("also_seen_in") or [])
                if current.get("source") and current["source"] not in seen:
                    seen.append(current["source"])
            else:
                seen = []
            winner = dict(receipt)
            winner["_score"] = value
            if seen:
                winner["also_seen_in"] = seen
            best[key] = winner
        elif current.get("source") and current["source"] != receipt.get("source"):
            seen = list(current.get("also_seen_in") or [])
            if receipt.get("source") not in seen:
                seen.append(str(receipt.get("source")))
                current["also_seen_in"] = seen
    merged = [{k: v for k, v in r.items() if k != "_score"} for r in best.values()]
    return rank_receipts(merged, terms=terms, now=now, limit=limit)


def build_citation(receipt: dict[str, Any]) -> dict[str, Any]:
    """Build the citation a receipt is quoted with.

    Args:
        receipt: a canonical receipt dict.

    Returns:
        ``{"source", "ref", "title", "uri", "label"}``.  ``label`` is the
        human-readable ``"[session] abc — fix login"`` form used in the answer
        body, so an answer line always names its source.

    Example:
        >>> build_citation({"source": "session", "ref": "s1", "title": "t"})["label"]
        '[session] s1 — t'
    """
    if not isinstance(receipt, dict):
        return {"source": "", "ref": "", "title": "", "uri": "", "label": ""}
    source = str(receipt.get("source") or "")
    ref = str(receipt.get("ref") or "")
    title = str(receipt.get("title") or "")
    label = f"[{source or 'unknown'}]"
    if ref:
        label += f" {ref}"
    if title:
        label += f" — {title}"
    return {
        "source": source,
        "ref": ref,
        "title": title,
        "uri": str(receipt.get("uri") or ""),
        "label": label,
    }


def build_answer(receipts: list[dict[str, Any]], *, query: str = "") -> dict[str, Any]:
    """Render ranked receipts into a cited answer.

    Args:
        receipts: already-ranked receipts (from :func:`merge_receipts`).
        query: the original query, echoed for traceability.

    Returns:
        ``{"query", "answer", "receipts", "cited"}`` where ``answer`` is one
        line per receipt, each prefixed by its citation label.  An empty
        receipt list yields ``cited=False`` and an explicit "no prior record"
        answer — never an uncited claim.

    Example:
        >>> build_answer([], query="anything")["cited"]
        False
    """
    if not receipts:
        return {
            "query": query,
            "answer": "No prior record found for this query.",
            "receipts": [],
            "cited": False,
        }
    lines = []
    for index, receipt in enumerate(receipts, start=1):
        citation = receipt.get("citation") or build_citation(receipt)
        snippet = str(receipt.get("text") or "").strip().replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:240] + "…"
        label = citation.get("label") or "[unknown]"
        uri = citation.get("uri") or ""
        link = f" <{uri}>" if uri else ""
        lines.append(f"{index}. {label}{link} — {snippet}" if snippet else f"{index}. {label}{link}")
    return {
        "query": query,
        "answer": "\n".join(lines),
        "receipts": list(receipts),
        "cited": True,
    }