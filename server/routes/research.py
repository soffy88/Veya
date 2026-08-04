"""POST /research — M-10 web_research_task: search + fetch + LLM synthesis"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/research", tags=["research"])


class ResearchRequest(BaseModel):
    query: str
    max_pages: int = 5


async def _researcher(query: str, max_pages: int = 5) -> dict[str, Any]:
    """hicode.compat-based researcher: web_search → http_fetch snippets."""
    import inspect as _insp

    from hicode.compat import http_fetch, web_search

    try:
        raw = web_search(query=query, max_results=max_pages)
        if _insp.isawaitable(raw):
            raw = await raw
    except Exception:
        raw = []

    results = raw if isinstance(raw, list) else []
    snippets = []
    urls = []
    for item in results[:max_pages]:
        url = item.get("url", "") if isinstance(item, dict) else ""
        if url:
            urls.append(url)
            try:
                page = http_fetch(url=url, timeout=10)
                if _insp.isawaitable(page):
                    page = await page
                text = (
                    (page.get("text") or page.get("content") or "")[:1000]
                    if isinstance(page, dict)
                    else str(page)[:1000]
                )
                snippets.append(text)
            except Exception:
                snippets.append(item.get("snippet", "") if isinstance(item, dict) else "")
    return {"snippets": snippets, "urls": urls}


@router.post("")
async def research_route(req: ResearchRequest) -> dict[str, Any]:
    import tempfile

    from hicode.compat import web_search

    with tempfile.TemporaryDirectory():
        # Use compat shim for web research
        result = await web_search(query=req.query, max_results=req.max_pages)

    if result.get("status") == "failed":
        raise HTTPException(status_code=502, detail=result.get("error"))

    return {
        "status": "success",
        "query": req.query,
        "report_path": result.get("report_path"),
        "cost_usd": result.get("cost_usd", 0.0),
        "summary": result.get("summary") or result.get("report"),
    }
