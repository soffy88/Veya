"""POST /research — M-10 web_research_task: search + fetch + LLM synthesis"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/research", tags=["research"])


class ResearchRequest(BaseModel):
    query: str
    max_pages: int = 5


async def _researcher(query: str, max_pages: int = 5) -> dict[str, Any]:
    """oprim-based researcher: web_search → http_fetch snippets."""
    from oprim import web_search, http_fetch
    import inspect as _insp

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
                text = (page.get("text") or page.get("content") or "")[:1000] if isinstance(page, dict) else str(page)[:1000]
                snippets.append(text)
            except Exception:
                snippets.append(item.get("snippet", "") if isinstance(item, dict) else "")
    return {"snippets": snippets, "urls": urls}


@router.post("")
async def research_route(req: ResearchRequest) -> dict[str, Any]:
    from omodul.web_research_task import web_research_task, Config, InputData
    from server.assembly import _llm_caller_adapter
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        config = Config(max_pages=req.max_pages)
        input_data = InputData(
            query=req.query,
            researcher=_researcher,
            llm_caller=_llm_caller_adapter,
        )
        result = await web_research_task(config, input_data, output_dir)

    if result.get("status") == "failed":
        raise HTTPException(status_code=502, detail=result.get("error"))

    return {
        "status": "success",
        "query": req.query,
        "report_path": result.get("report_path"),
        "cost_usd": result.get("cost_usd", 0.0),
        "summary": result.get("summary") or result.get("report"),
    }
