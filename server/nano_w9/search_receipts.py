"""server/nano_w9/search_receipts — "have we seen this before?" with receipts.

NANO-W9 element 5.  Unified search across sessions (W2 FTS), GoalRun results,
group messages, approvals and annotations, answering with **citations** to the
source threads.

Layer split:

* :mod:`veya.oskill.pure.receipt_rank_merge` — deterministic rank / merge /
  citation (pure, no IO).
* :mod:`veya.oprim.receipt_query` — the canonical receipt shape and the
  per-source query normalisation (one source per call).
* this module — layer 4: binds the existing stores to per-source callables and
  assembles the **existing** ``oservi.ResearcherEngine`` (``on_demand``) so the
  mechanism (fetch → merge → answer) is reused rather than rewritten.

Identity: the caller is expected to have pseudonymised the user id before this
point (§5.5.1); the search narrows by that reference, never by a raw user id.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from veya.oprim.receipt_query import RECEIPT_SOURCES, normalize_query, normalize_receipt
from veya.oskill.pure.receipt_rank_merge import build_answer, merge_receipts

__all__ = [
    "RECEIPT_SOURCES",
    "ReceiptSearch",
    "build_researcher_manifest",
    "normalize_query",
    "normalize_receipt",
]


def build_researcher_manifest(
    *,
    name: str,
    search: Callable[..., Any],
    fetch: Callable[..., Any] | None = None,
    config: dict[str, Any] | None = None,
) -> Any:
    """Assemble the **existing** ``oservi.ResearcherEngine`` via a ServiceManifest.

    Args:
        name: manifest/service name.
        search: per-source query callable (injected ``oprim`` slot).
        fetch: optional full-text fetcher (injected ``oprim`` slot).
        config: engine config.

    Returns:
        A ``ServiceManifest``, or ``None`` when the external oservi library is not
        mounted — the caller then runs :class:`ReceiptSearch` directly, which
        behaves identically (same per-source queries, same pure merge).
    """
    try:
        from veya.platform import load

        oservi = load("oservi")
    except Exception:
        return None
    inject: dict[str, list[Callable[..., Any]]] = {"search": [search]}
    if fetch is not None:
        inject["fetch"] = [fetch]
    return oservi.ServiceManifest(
        name=name,
        skeleton="researcher",
        inject=inject,
        trigger={"on_demand": True},
        config=dict(config or {}),
    )


class ReceiptSearch:
    """Unified, cited search over the existing Veya stores.

    Args:
        sources: ``{source_name: query_callable(query, *, limit, since, ref)}``.
            A missing source is simply skipped (never an error).
        ref: the (already pseudonymised) user/scope reference to scope by.
        manifest: optional pre-built researcher manifest.
    """

    def __init__(
        self,
        *,
        sources: dict[str, Callable[..., Any]] | None = None,
        ref: str = "",
        manifest: Any = None,
        now: float = 0.0,
    ) -> None:
        self.sources = dict(sources or {})
        self.ref = ref
        self.now = now
        self.manifest = (
            manifest
            if manifest is not None
            else build_researcher_manifest(name="receipt-search", search=self._query_one)
        )

    # -- per-source query (one source per call) ---------------------------

    def _query_one(
        self, query: dict[str, Any], *, limit: int = 20, since: float = 0.0, ref: str = ""
    ) -> list[dict[str, Any]]:
        """Ask one source. Returns canonical receipts (possibly empty)."""
        source = str(query.get("source") or "")
        fn = self.sources.get(source)
        if fn is None:
            return []
        try:
            rows = fn(
                query.get("query", ""),
                limit=limit,
                since=since,
                ref=ref or self.ref,
            )
        except TypeError:
            rows = fn(query.get("query", ""), limit)
        except Exception:
            return []
        if not rows:
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict) and "receipt_id" in row:
                out.append(dict(row))
            else:
                out.append(normalize_receipt(source, row if isinstance(row, dict) else {}))
        return out

    # -- public API --------------------------------------------------------

    def query_all(self, query: str, *, limit: int = 20, since: float = 0.0) -> list[dict[str, Any]]:
        """Run every configured source and return the merged, cited ranking.

        Args:
            query: the user's question.
            limit: per-source cap (also the merged cap).
            since: unix timestamp lower bound.

        Returns:
            Ranked receipts, each carrying ``score`` and ``citation``.
        """
        normalized = normalize_query(query, limit=limit, since=since)
        collected: list[dict[str, Any]] = []
        for source in RECEIPT_SOURCES:
            collected.extend(
                self._query_one(
                    {"source": source, "query": normalized["query"]},
                    limit=normalized["limit"],
                    since=normalized["since"],
                )
            )
        return merge_receipts(
            collected,
            terms=normalized["terms"],
            now=self.now,
            limit=normalized["limit"],
        )

    def answer(self, query: str, *, limit: int = 20, since: float = 0.0) -> dict[str, Any]:
        """Answer "have we seen this before?" with a cited answer.

        Args:
            query: the user's question.
            limit: per-source cap.
            since: unix timestamp lower bound.

        Returns:
            ``{"status", "query", "answer", "receipts", "cited", "sources",
            "errors"}``.  ``cited=False`` whenever no receipt was found — the
            answer says so explicitly instead of asserting something unsourced.
        """
        normalized = normalize_query(query, limit=limit, since=since)
        collected: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        asked: list[str] = []
        for source in RECEIPT_SOURCES:
            if source not in self.sources:
                continue
            asked.append(source)
            try:
                rows = self.sources[source](
                    normalized["query"],
                    limit=normalized["limit"],
                    since=normalized["since"],
                    ref=self.ref,
                )
            except Exception as exc:
                errors.append({"source": source, "reason": str(exc)})
                continue
            for row in rows or []:
                if isinstance(row, dict) and "receipt_id" in row:
                    collected.append(dict(row))
                else:
                    collected.append(
                        normalize_receipt(source, row if isinstance(row, dict) else {})
                    )
        merged = merge_receipts(
            collected,
            terms=normalized["terms"],
            now=self.now,
            limit=normalized["limit"],
        )
        rendered = build_answer(merged, query=normalized["query"])
        return {
            "status": "ok" if not errors else "partial",
            "query": normalized["query"],
            "answer": rendered["answer"],
            "receipts": merged,
            "cited": rendered["cited"],
            "sources": asked,
            "errors": errors,
        }


def default_sources(
    *,
    history_store: Any = None,
    event_store: Any = None,
    groups: Any = None,
    approvals: Any = None,
    annotations: Any = None,
    project_root: str = ".",
) -> dict[str, Callable[..., Any]]:
    """Build the per-source callables over the **existing** Veya stores.

    Each callable has the shape ``fn(query, *, limit, since, ref) -> list[dict]``
    and returns *raw* store rows; :meth:`ReceiptSearch` normalises them.  Any
    store that is not supplied is simply absent from the mapping, so search
    degrades to fewer sources instead of failing.

    Args:
        history_store: W2 FTS store (anything with ``search_sync`` / ``search``).
        event_store: the existing JSONL event store.
        groups: a P2 ``GroupStore``.
        approvals: anything with ``list_approvals`` / iterable records.
        annotations: a callable ``(project_root, goal_id) -> list[dict]``.
        project_root: project root used by the annotation source.

    Returns:
        ``{source_name: callable}`` for the sources that could be wired.
    """
    sources: dict[str, Callable[..., Any]] = {}

    if history_store is not None:
        searcher = getattr(history_store, "search_sync", None) or getattr(
            history_store, "search", None
        )
        if callable(searcher):

            def _sessions(
                query: str, *, limit: int, since: float, ref: str
            ) -> list[dict[str, Any]]:
                rows = searcher(query, ref or None, limit=limit)
                return [
                    {
                        "ref": str(row.get("sid") or ""),
                        "title": str(row.get("title") or ""),
                        "text": str(row.get("snippet") or ""),
                        "ts": float(row.get("ts") or 0.0),
                    }
                    for row in rows or []
                    if isinstance(row, dict)
                ]

            sources["session"] = _sessions

    if event_store is not None:

        def _goal_runs(query: str, *, limit: int, since: float, ref: str) -> list[dict[str, Any]]:
            needle = query.strip().lower()
            out: list[dict[str, Any]] = []
            for event in event_store.read_all(topics={"goal.completed"}) or []:
                if needle and needle not in str(event.get("topic", "")).lower():
                    payload = event.get("payload") or {}
                    hay = " ".join(
                        str(payload.get(k, "")) for k in ("goal_id", "goal_event", "summary")
                    ).lower()
                    if needle not in hay:
                        continue
                out.append(
                    {
                        "ref": str((event.get("payload") or {}).get("goal_id") or ""),
                        "title": str((event.get("payload") or {}).get("goal_id") or "goal"),
                        "text": json_dumps(event.get("payload") or {}),
                        "ts": float(event.get("ts") or 0.0),
                    }
                )
                if len(out) >= limit:
                    break
            return out

        sources["goal_run"] = _goal_runs

    if groups is not None:

        def _group_messages(
            query: str, *, limit: int, since: float, ref: str
        ) -> list[dict[str, Any]]:
            needle = query.strip().lower()
            out: list[dict[str, Any]] = []
            for group in (groups.list() or [])[:20]:
                for message in groups.messages(group.id) or []:
                    if needle and needle not in str(message.get("text", "")).lower():
                        continue
                    out.append(
                        {
                            "ref": str(group.id),
                            "title": str(group.name),
                            "text": str(message.get("text", ""))[:400],
                            "ts": float(message.get("ts") or 0.0),
                        }
                    )
                    if len(out) >= limit:
                        return out
            return out

        sources["group_message"] = _group_messages

    if approvals is not None:
        lister = getattr(approvals, "list_approvals", None) or (
            approvals if callable(approvals) else None
        )
        if callable(lister):

            def _approvals_source(
                query: str, *, limit: int, since: float, ref: str
            ) -> list[dict[str, Any]]:
                needle = query.strip().lower()
                out: list[dict[str, Any]] = []
                for record in lister() or []:
                    data = record if isinstance(record, dict) else getattr(record, "__dict__", {})
                    action = str(data.get("action") or "")
                    if needle and needle not in action.lower():
                        continue
                    out.append(
                        {
                            "ref": str(data.get("request_id") or ""),
                            "title": action,
                            "text": f"status={data.get('status', '')}",
                            "ts": float(data.get("created_at") or 0.0),
                        }
                    )
                    if len(out) >= limit:
                        break
                return out

            sources["approval"] = _approvals_source

    if annotations is not None:

        def _annotations_source(
            query: str, *, limit: int, since: float, ref: str
        ) -> list[dict[str, Any]]:
            needle = query.strip().lower()
            out: list[dict[str, Any]] = []
            for annotation in annotations(project_root, ref) or []:
                body = str(annotation.get("body", ""))
                location = f"{annotation.get('file', '')}:{annotation.get('line', 0)}"
                if needle and needle not in f"{body} {location}".lower():
                    continue
                out.append(
                    {
                        "ref": str(annotation.get("id") or ""),
                        "title": location,
                        "text": body,
                        "ts": float(annotation.get("created_at") or 0.0),
                    }
                )
                if len(out) >= limit:
                    break
            return out

        sources["annotation"] = _annotations_source

    return sources


def json_dumps(value: Any) -> str:
    """Compact, deterministic JSON for a receipt body."""
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)[:400]
