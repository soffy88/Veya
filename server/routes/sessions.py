"""GET /sessions — session list with optional ?project= filter."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("")
async def list_sessions(project: str | None = None) -> dict[str, Any]:
    from server.routes.session import _sessions

    result = []
    for s in _sessions.values():
        s_project = s.get("project", "default")
        if project is not None and s_project != project:
            continue

        msgs = s.get("messages", [])
        title = _title_from_messages(msgs)
        result.append({
            "id": s["id"],
            "project": s_project,
            "persona": s.get("persona", "build"),
            "title": title,
            "cost_usd": s.get("cost_usd", 0.0),
            "message_count": len(msgs),
            "updated_at": s.get("updated_at", s.get("created_at", "2024-01-01T00:00:00Z")),
        })

    return {"sessions": result, "total": len(result)}


def _title_from_messages(msgs: list[dict]) -> str:
    for m in msgs:
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            return content[:50].replace("\n", " ") or "Untitled"
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    return text[:50].replace("\n", " ") or "Untitled"
    return "Untitled"
