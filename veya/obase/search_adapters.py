"""veya/obase/search_adapters.py — ADDENDUM T: search adapters.

External adapters for search backends:
- configured_provider: configured search API (Tavily, Brave, etc.)
- model_native: model route native search capability
- duckduckgo: keyless public search
- bing: keyless public search

obase layer: external interface adaptation only. NEVER imports 3O layers (oprim, oskill, etc.).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any
from urllib.parse import quote_plus

import httpx

_log = logging.getLogger("veya.obase.search_adapters")

__all__ = [
    "SEARCH_LADDER_ORDER",
    "execute_search_backend",
    "run_search_ladder_adapter",
    "search_bing",
    "search_configured_provider",
    "search_duckduckgo",
    "search_model_native",
]

SEARCH_LADDER_ORDER: tuple[str, ...] = (
    "configured_provider",
    "model_native",
    "duckduckgo",
    "bing",
)

_FAILURE_NOTICE: str = "Search failed across all backends. Do not guess or fabricate URLs."


async def search_configured_provider(
    query: str,
    *,
    timeout: float = 5.0,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Query configured search API (Tavily, Brave, SearXNG, etc.) if key/endpoint present."""
    api_key = (
        os.environ.get("TAVILY_API_KEY")
        or os.environ.get("BRAVE_API_KEY")
        or os.environ.get("SEARCH_API_KEY")
        or ""
    )
    if not api_key:
        return []

    # Example integration with Tavily if TAVILY_API_KEY is present
    if os.environ.get("TAVILY_API_KEY"):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.post(
                    "https://api.tavily.com/search",
                    json={"query": query, "api_key": api_key, "max_results": 5},
                )
                if res.status_code == 200:
                    data = res.json()
                    hits = []
                    for r in data.get("results", []):
                        hits.append({
                            "url": r.get("url", ""),
                            "title": r.get("title", ""),
                            "snippet": r.get("content", ""),
                            "backend": "configured_provider",
                        })
                    return [h for h in hits if h["url"]]
        except Exception as exc:
            _log.debug("configured_provider search failed: %s", exc)

    return []


async def search_model_native(
    query: str,
    *,
    native_search_fn: Any = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Query active model route native search capability if injected."""
    if native_search_fn is not None:
        try:
            if asyncio.iscoroutinefunction(native_search_fn):
                res = await native_search_fn(query)
            else:
                res = native_search_fn(query)
            if isinstance(res, list):
                return [
                    {
                        "url": r.get("url", ""),
                        "title": r.get("title", ""),
                        "snippet": r.get("snippet", ""),
                        "backend": "model_native",
                    }
                    for r in res
                    if isinstance(r, dict) and r.get("url")
                ]
        except Exception as exc:
            _log.debug("model_native search failed: %s", exc)

    return []


async def search_duckduckgo(
    query: str,
    *,
    timeout: float = 5.0,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Keyless public search via DuckDuckGo lite/html endpoint."""
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return []

            # Parse results from HTML using regex (dependency-free)
            hits: list[dict[str, Any]] = []
            links = re.findall(
                r'<a[^>]+class="result__url"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                resp.text,
                re.DOTALL,
            )
            for raw_href, raw_title in links[:5]:
                # Extract clean target URL if wrapped in DuckDuckGo redirect
                target = raw_href
                m = re.search(r"uddg=([^&]+)", raw_href)
                if m:
                    from urllib.parse import unquote

                    target = unquote(m.group(1))

                clean_title = re.sub(r"<[^>]+>", "", raw_title).strip()
                if target.startswith("http"):
                    hits.append({
                        "url": target,
                        "title": clean_title,
                        "snippet": "",
                        "backend": "duckduckgo",
                    })
            return hits
    except Exception as exc:
        _log.debug("duckduckgo search failed: %s", exc)
        return []


async def search_bing(
    query: str,
    *,
    timeout: float = 5.0,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Keyless public search via Bing html endpoint."""
    url = f"https://www.bing.com/search?q={quote_plus(query)}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return []

            hits: list[dict[str, Any]] = []
            # Match <h2><a href="...">Title</a></h2>
            matches = re.findall(
                r'<h2><a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a></h2>',
                resp.text,
                re.DOTALL,
            )
            for target, raw_title in matches[:5]:
                clean_title = re.sub(r"<[^>]+>", "", raw_title).strip()
                hits.append({
                    "url": target,
                    "title": clean_title,
                    "snippet": "",
                    "backend": "bing",
                })
            return hits
    except Exception as exc:
        _log.debug("bing search failed: %s", exc)
        return []


async def execute_search_backend(backend: str, query: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Execute a single search backend by name."""
    if backend == "configured_provider":
        return await search_configured_provider(query, **kwargs)
    if backend == "model_native":
        return await search_model_native(query, **kwargs)
    if backend == "duckduckgo":
        return await search_duckduckgo(query, **kwargs)
    if backend == "bing":
        return await search_bing(query, **kwargs)
    return []


async def run_search_ladder_adapter(
    query: str,
    *,
    backends: tuple[str, ...] = SEARCH_LADDER_ORDER,
    **kwargs: Any,
) -> tuple[str, list[dict[str, Any]], str]:
    """Execute the search ladder in order until hits are found.

    Returns:
        (winning_backend, hits, formatted_text)
    """
    for backend in backends:
        try:
            hits = await execute_search_backend(backend, query, **kwargs)
            if hits:
                # Format output with Search backend: <name>
                lines = []
                for h in hits:
                    lines.append(f"Search backend: {backend}")
                    if h.get("title"):
                        lines.append(f"Title: {h['title']}")
                    lines.append(f"URL: {h['url']}")
                    if h.get("snippet"):
                        lines.append(f"Snippet: {h['snippet']}")
                    lines.append("")
                return backend, hits, "\n".join(lines).strip()
        except Exception as exc:
            _log.debug("Search backend %s failed: %s", backend, exc)

    return "none", [], _FAILURE_NOTICE
