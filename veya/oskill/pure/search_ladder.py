"""veya/oskill/pure/search_ladder.py — ADDENDUM T: search fallback ladder.

Pure logic for falling back across search backends when the primary returns
nothing (or errors).

Search order (ADDENDUM T):
    configured provider -> current model route native search -> keyless public result pages (DuckDuckGo, Bing) -> fail with instruction not to guess URLs

Requirements:
1. plan_search: order available backends according to search ladder.
2. merge_search_results: deduplicate results by normalized URL, preserving order.
3. format_search_results: each result line prepends 'Search backend: <name>'.
4. fail message: if all backends fail, instruct the model not to guess URLs.

Zero IO, zero network, pure logic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "DEFAULT_SEARCH_BACKENDS",
    "SEARCH_FAILURE_NOTICE",
    "SearchHit",
    "SearchLadderError",
    "SearchPlan",
    "format_search_results",
    "merge_search_results",
    "plan_search",
    "should_fall_back",
]

# Most-preferred → least-preferred per ADDENDUM T spec.
DEFAULT_SEARCH_BACKENDS: tuple[str, ...] = (
    "configured_provider",
    "model_native",
    "duckduckgo",
    "bing",
)

SEARCH_FAILURE_NOTICE: str = "Search failed across all backends. Do not guess or fabricate URLs."


class SearchLadderError(ValueError):
    """Raised when no search backend is available."""


@dataclass(frozen=True)
class SearchHit:
    """One search result, normalized across backends."""

    url: str
    title: str = ""
    snippet: str = ""
    backend: str = ""
    rank: int = 0

    def key(self) -> str:
        return self.url.strip().rstrip("/")


@dataclass(frozen=True)
class SearchPlan:
    backend: str
    fallback_backends: tuple[str, ...]
    reason: str

    def next_after_failure(self) -> SearchPlan | None:
        if not self.fallback_backends:
            return None
        head, *rest = self.fallback_backends
        return SearchPlan(
            backend=head,
            fallback_backends=tuple(rest),
            reason=f"fallback after {self.backend}",
        )


def plan_search(
    available: Mapping[str, bool] | Sequence[str],
    *,
    preference: Sequence[str] = DEFAULT_SEARCH_BACKENDS,
) -> SearchPlan:
    """Choose the most-preferred available backend and its fallback chain.

    ``available`` accepts either a mapping of backend → bool, or a sequence of
    backend names that are considered available.
    """
    if isinstance(available, Mapping):
        avail_set = {name for name, ok in available.items() if ok}
    else:
        avail_set = set(available)
    known = [b for b in preference if b in avail_set]
    if not known:
        raise SearchLadderError(
            "no search backend available (preference=" + ", ".join(preference) + ")"
        )
    return SearchPlan(
        backend=known[0],
        fallback_backends=tuple(known[1:]),
        reason="search ladder",
    )


def should_fall_back(hits: Sequence[SearchHit], *, min_hits: int = 1) -> bool:
    """A backend is considered failed when it returns fewer than ``min_hits``."""
    return len([h for h in hits if h.key()]) < max(0, min_hits)


def merge_search_results(
    *result_groups: Iterable[SearchHit],
    limit: int | None = None,
    per_backend_cap: int | None = None,
) -> list[SearchHit]:
    """Merge backends in precedence order, deduping by normalized URL.

    ``result_groups`` must already be ordered most-preferred first. Within a
    group, order is preserved. ``limit`` caps the total; ``per_backend_cap``
    caps how many hits each backend may contribute after dedupe.
    """
    seen: set[str] = set()
    merged: list[SearchHit] = []
    for group in result_groups:
        taken = 0
        for hit in group:
            key = hit.key()
            if not key or key in seen:
                continue
            if per_backend_cap is not None and taken >= per_backend_cap:
                break
            seen.add(key)
            merged.append(hit)
            taken += 1
            if limit is not None and len(merged) >= limit:
                return merged
    return merged


def format_search_results(
    hits: Sequence[SearchHit],
    backend: str | None = None,
) -> str:
    """Format search hits for agent/model consumption.

    Each result block prepends a 'Search backend: <name>' line.
    If no valid hits are provided, returns the instruction not to guess URLs.
    """
    valid_hits = [h for h in hits if h.url.strip()]
    if not valid_hits:
        return SEARCH_FAILURE_NOTICE

    sections: list[str] = []
    for hit in valid_hits:
        b = hit.backend or backend or "unknown"
        lines = [f"Search backend: {b}"]
        if hit.title:
            lines.append(f"Title: {hit.title}")
        lines.append(f"URL: {hit.url}")
        if hit.snippet:
            lines.append(f"Snippet: {hit.snippet}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)
