"""Session search endpoints with FTS5."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from server import auth as auth_mod

router = APIRouter(prefix="/api/v1/sessions", tags=["session-search"])


class SearchResult(BaseModel):
    sid: str
    revision: int
    snippet: str
    ts: int
    title: str
    backend: str | None = None


class FederatedResult(BaseModel):
    runner_id: str
    results: list[SearchResult]
    error: str | None = None


@router.get("/search")
async def search_sessions(
    q: str = Query(..., min_length=1, description="Search query"),
    backend: str | None = Query(None, description="Filter by backend"),
    from_date: str | None = Query(None, description="Start date (YYYY-MM-DD)"),
    to_date: str | None = Query(None, description="End date (YYYY-MM-DD)"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    federated: bool = Query(False, description="Include federated runner results"),
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Full-text search over user's session transcripts."""
    from veya.history_store import default_history_store

    store = default_history_store()

    # Parse date filters
    from_ts = None
    to_ts = None
    if from_date:
        try:
            from_ts = int(datetime.strptime(from_date, "%Y-%m-%d").timestamp())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid from_date format, use YYYY-MM-DD")
    if to_date:
        try:
            to_ts = int(datetime.strptime(to_date, "%Y-%m-%d").timestamp()) + 86399  # End of day
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid to_date format, use YYYY-MM-DD")

    # Local search
    local_results = await store.search(
        query=q,
        user_id=user["user_id"],
        backend=backend,
        from_ts=from_ts,
        to_ts=to_ts,
        limit=limit,
    )

    # Convert to SearchResult models
    results = [
        SearchResult(
            sid=r["sid"],
            revision=r["revision"],
            snippet=r["snippet"],
            ts=r["ts"],
            title=r["title"],
        )
        for r in local_results
    ]

    response = {
        "query": q,
        "results": results,
        "count": len(results),
        "user_id": user["user_id"],
    }

    # Federated search (optional, non-blocking)
    if federated:
        federated_results = await _federated_search(
            q, user["user_id"], backend, from_ts, to_ts, limit
        )
        response["federated"] = federated_results

    return response


async def _federated_search(
    query: str,
    user_id: str,
    backend: str | None,
    from_ts: int | None,
    to_ts: int | None,
    limit: int,
) -> list[FederatedResult]:
    """Query configured runners with timeout; failures don't break local results."""
    runners = _get_runners()
    if not runners:
        return []

    async def query_runner(runner: dict[str, Any]) -> FederatedResult:
        runner_id = runner.get("id", "unknown")
        url = runner.get("url")
        if not url:
            return FederatedResult(runner_id=runner_id, results=[], error="No URL configured")

        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as client:
                params = {"q": query, "limit": limit}
                if backend:
                    params["backend"] = backend
                if from_ts:
                    params["from_ts"] = from_ts
                if to_ts:
                    params["to_ts"] = to_ts

                resp = await client.get(f"{url}/api/v1/sessions/search", params=params)
                if resp.is_success:
                    data = resp.json()
                    results = [
                        SearchResult(
                            sid=r["sid"],
                            revision=r["revision"],
                            snippet=r["snippet"],
                            ts=r["ts"],
                            title=r["title"],
                        )
                        for r in data.get("results", [])
                    ]
                    return FederatedResult(runner_id=runner_id, results=results)
                else:
                    return FederatedResult(
                        runner_id=runner_id, results=[], error=f"HTTP {resp.status_code}"
                    )
        except TimeoutError:
            return FederatedResult(runner_id=runner_id, results=[], error="Timeout")
        except Exception as e:
            return FederatedResult(runner_id=runner_id, results=[], error=str(e))

    tasks = [query_runner(r) for r in runners]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    federated_results: list[FederatedResult] = []
    for r in results:
        if isinstance(r, FederatedResult):
            federated_results.append(r)
        elif isinstance(r, Exception):
            federated_results.append(FederatedResult(runner_id="unknown", results=[], error=str(r)))

    return federated_results


def _get_runners() -> list[dict[str, Any]]:
    """Get configured runners from bot_team/runners.py if available."""
    try:
        from server.bot_team.runners import get_runners

        return get_runners()
    except ImportError:
        return []


class RebuildRequest(BaseModel):
    user_id: str | None = None


@router.post("/search/rebuild")
async def rebuild_fts(
    req: RebuildRequest | None = None,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Rebuild FTS5 index from existing turns data."""
    from veya.history_store import default_history_store

    store = default_history_store()
    # Only rebuild for current user unless admin (not implemented)
    target_user = req.user_id if req and req.user_id else user["user_id"]
    if target_user != user["user_id"]:
        raise HTTPException(status_code=403, detail="Cannot rebuild index for other users")

    result = await store.rebuild_fts(user_id=target_user)
    return result
