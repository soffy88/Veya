"""Legacy session routes plus the canonical Unified Session API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.events import event_store
from server.session_identity import new_session_id

router = APIRouter(prefix="/sessions", tags=["sessions"])
unified_router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


class UnifiedSessionCreateRequest(BaseModel):
    session_id: str | None = None


class UnifiedSessionResumeRequest(BaseModel):
    text: str | None = Field(None, min_length=1)
    max_rounds: int | None = Field(None, ge=1, le=100)


@router.get("")
async def list_sessions(
    project: str | None = None, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    from server.routes.session import _sessions

    result = []
    for s in _sessions.values():
        if s.get("user_id", "anonymous") != user["user_id"]:
            continue
        s_project = s.get("project", "default")
        if project is not None and s_project != project:
            continue

        msgs = s.get("messages", [])
        title = _title_from_messages(msgs)
        result.append(
            {
                "id": s["id"],
                "project": s_project,
                "persona": s.get("persona", "build"),
                "title": title,
                "cost_usd": s.get("cost_usd", 0.0),
                "message_count": len(msgs),
                "updated_at": s.get("updated_at", s.get("created_at", "2024-01-01T00:00:00Z")),
            }
        )

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


async def _unified_session(
    session_id: str, user: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load one canonical session and enforce history-store ownership."""
    from veya.history_store import default_history_store

    store = default_history_store()
    entries = await store.list_sessions(user_id=user["user_id"], limit=5000)
    metadata = next((entry for entry in entries if entry["sid"] == session_id), None)
    if metadata is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    messages = await store.load(session_id, user_id=user["user_id"])
    from server.coordinator_master import _active_streams

    active_task = _active_streams.get(session_id)
    active = active_task is not None and not active_task.done()
    return (
        {
            "session_id": session_id,
            "title": metadata.get("title") or "Untitled",
            "message_count": len(messages),
            "updated_at": metadata.get("updated_at"),
            "active": active,
        },
        messages,
    )


@unified_router.get("")
async def list_unified_sessions(
    limit: int = 50, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    """List sessions from the same durable store used by MasterAgent."""
    from veya.history_store import default_history_store

    sessions = await default_history_store().list_sessions(
        user_id=user["user_id"], limit=max(1, min(limit, 500))
    )
    return {
        "sessions": [
            {
                "session_id": item["sid"],
                "title": item.get("title") or "Untitled",
                "message_count": item.get("msg_count", 0),
                "updated_at": item.get("updated_at"),
            }
            for item in sessions
        ],
        "count": len(sessions),
        "user_id": user["user_id"],
    }


@unified_router.post("")
async def create_unified_session(
    req: UnifiedSessionCreateRequest | None = None,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Create an empty durable session that every entry point can reuse."""
    from veya.history_store import default_history_store

    requested_id = req.session_id if req is not None else None
    session_id = requested_id or new_session_id()
    store = default_history_store()
    existing = await store.list_sessions(user_id=user["user_id"], limit=5000)
    if any(item["sid"] == session_id for item in existing):
        raise HTTPException(status_code=409, detail=f"session already exists: {session_id}")
    await store.save(session_id, [], user_id=user["user_id"])
    event_store.append(
        {
            "topic": "session.created",
            "session_id": session_id,
            "trace_id": session_id,
            "actor": user["user_id"],
            "payload": {"session_id": session_id},
        }
    )
    return {"session_id": session_id, "status": "created", "messages": []}


@unified_router.get("/{session_id}")
async def get_unified_session(
    session_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    metadata, messages = await _unified_session(session_id, user)
    return {**metadata, "messages": messages}


@unified_router.get("/{session_id}/events")
async def get_unified_session_events(
    session_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    await _unified_session(session_id, user)
    return {"session_id": session_id, "events": event_store.read_all(session_id=session_id)}


@unified_router.post("/{session_id}/attach")
async def attach_unified_session(
    session_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    metadata, messages = await _unified_session(session_id, user)
    return {**metadata, "messages": messages, "attached": True}


@unified_router.post("/{session_id}/resume")
async def resume_unified_session(
    session_id: str,
    req: UnifiedSessionResumeRequest | None = None,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Resume durable MasterAgent context, optionally executing a new turn."""
    metadata, messages = await _unified_session(session_id, user)
    text = req.text if req is not None else None
    if not text:
        event_store.append(
            {
                "topic": "resume.completed",
                "session_id": session_id,
                "trace_id": session_id,
                "actor": user["user_id"],
                "payload": {"status": "ready"},
            }
        )
        return {**metadata, "messages": messages, "resumed": True, "status": "ready"}
    if metadata["active"]:
        raise HTTPException(status_code=409, detail="session already has an active turn")
    from server.coordinator_master import master_coordinator

    event_store.append(
        {
            "topic": "resume.started",
            "session_id": session_id,
            "trace_id": session_id,
            "actor": user["user_id"],
            "payload": {"text_length": len(text)},
        }
    )
    try:
        result = await master_coordinator.chat_stream(
            text, session_id=session_id, max_rounds=req.max_rounds if req else None
        )
    except Exception as exc:
        event_store.append(
            {
                "topic": "resume.failed",
                "session_id": session_id,
                "trace_id": session_id,
                "actor": user["user_id"],
                "payload": {"error": str(exc)},
            }
        )
        raise
    event_store.append(
        {
            "topic": "resume.completed",
            "session_id": session_id,
            "trace_id": result.get("trace_id") or session_id,
            "actor": user["user_id"],
            "payload": {"status": result.get("status")},
        }
    )
    result["resumed"] = True
    return result
