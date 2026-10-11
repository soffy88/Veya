"""veya/oskill/pure/search_ladder.py — ADDENDUM T: search fallback ladder.

Pure logic for falling back across search backends when the primary returns
nothing (or errors).  Two responsibilities:

1. :func:`plan_search` — order the *available* backends and expose the
   fallback chain.
2. :func:`merge_search_results` — dedupe + stably rank results gathered from
   multiple backends (URL is the identity key; first occurrence wins).

Backend names are opaque strings; callers register whatever they actually
have (e.g. ``"tavily"``, ``"brave"``, ``"searxng"``, ``"html_scrape"``).
A sensible default preference is provided by :data:`DEFAULT_SEARCH_BACKENDS`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "DEFAULT_SEARCH_BACKENDS",
    "SearchHit",
    "SearchPlan",
    "SearchLadderError",
    "plan_search",
    "merge_search_results",
    "should_fall_back",
]

# Most-preferred → least-preferred.  Callers may override entirely.
DEFAULT_SEARCH_BACKENDS: tuple[str, ...] = (
    "primary_api",
    "secondary_api",
    "html_scrape",
    "browser",
)


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

    def next_after_failure(self) -> "SearchPlan | None":
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
    """Merge backends in precedence order, deduping by URL.

    ``result_groups`` must already be ordered most-preferred first.  Within a
    group, order is preserved.  ``limit`` caps the total; ``per_backend_cap``
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
