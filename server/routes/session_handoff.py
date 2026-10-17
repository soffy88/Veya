"""Session handoff and fork endpoints."""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.backends import get_backend_registry
from server.events import event_store
from server.routes.session import _sessions
from server.session_identity import new_session_id

router = APIRouter(tags=["session-handoff"])


class HandoffRequest(BaseModel):
    target_backend: str
    target_model: str | None = None
    mode: str = Field(default="focused", pattern="^(focused|full)$")
    note: str | None = None


class ForkPolishRequest(BaseModel):
    label: str = ""
    polish: bool = True
    summary: str | None = None


def _owned_session(session_id: str, user: dict[str, Any]) -> dict[str, Any]:
    s = _sessions.get(session_id)
    owner = (s or {}).get("user_id", "anonymous")
    if not s or owner != user["user_id"]:
        raise HTTPException(status_code=404, detail="Session not found")
    return s


def _get_session_messages(session_id: str, user: dict[str, Any]) -> list[dict[str, Any]]:
    """Get messages from either in-memory _sessions or durable history_store."""
    s = _sessions.get(session_id)
    if s and s.get("messages"):
        return s["messages"]
    # Fall back to durable store
    from veya.history_store import default_history_store

    store = default_history_store()
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, store.load(session_id, user_id=user["user_id"]))
                return future.result()
        else:
            return asyncio.run(store.load(session_id, user_id=user["user_id"]))
    except Exception:
        return []


def generate_brief(
    session_id: str,
    messages: list[dict[str, Any]],
    mode: str,
    user: dict[str, Any],
) -> str:
    """Generate a handoff brief from session messages."""
    if not messages:
        return f"Session {session_id} — no messages yet."

    # Extract key information from messages
    user_messages = [m for m in messages if m.get("role") == "user"]
    assistant_messages = [m for m in messages if m.get("role") == "assistant"]

    first_user = user_messages[0].get("content", "") if user_messages else ""
    last_user = user_messages[-1].get("content", "") if user_messages else ""

    # Get files touched (look for tool calls in assistant messages)
    files_touched = set()
    for msg in assistant_messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    input_data = block.get("input", {})
                    if "path" in input_data:
                        files_touched.add(input_data["path"])

    # Get todos from messages (look for structured data)
    todos = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "todo":
                    todos.append(block.get("text", ""))

    now = datetime.utcnow().isoformat() + "Z"

    if mode == "full":
        # Full transcript
        lines = [
            "# Session Handoff — Full Transcript",
            f"Session: {session_id}",
            f"Generated: {now}",
            "",
        ]
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(b.get("text", str(b)) for b in content if isinstance(b, dict))
            lines.append(f"## {role.upper()}")
            lines.append(str(content))
            lines.append("")
        return "\n".join(lines)

    # Focused brief
    lines = [
        "# Session Handoff — Focused Brief",
        f"Session: {session_id}",
        f"Generated: {now}",
        "",
        "## Goal",
        first_user[:500] if first_user else "No goal stated",
        "",
        "## Constraints & Rules",
        "User-stated constraints extracted from conversation.",
        "",
        "## Current Progress",
        last_user[:500] if last_user else "No recent activity",
        "",
        "## Files Touched",
        "\n".join(f"- {f}" for f in sorted(files_touched)) or "No files tracked",
        "",
        "## Open Todos",
        "\n".join(f"- {t}" for t in todos) or "No todos tracked",
        "",
        "## Worktree Path",
        _sessions.get(session_id, {}).get("cwd", "Not set"),
        "",
        "## Reading Older History",
        f"Use GET /api/v1/sessions/{session_id} to fetch full message history.",
        f"Use GET /api/v1/sessions/{session_id}/events for event log.",
    ]
    return "\n".join(lines)


@router.get("/{session_id}/handoff/preview")
async def handoff_preview(
    session_id: str,
    mode: str = "focused",
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Preview the handoff brief before confirming."""
    _owned_session(session_id, user)
    messages = _get_session_messages(session_id, user)
    brief = generate_brief(session_id, messages, mode, user)
    return {
        "session_id": session_id,
        "mode": mode,
        "brief": brief,
        "warning": "Full mode includes entire transcript (slower, costs context)."
        if mode == "full"
        else None,
    }


@router.post("/{session_id}/handoff")
async def handoff_session(
    session_id: str,
    req: HandoffRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Hand off session to another backend/agent."""
    s = _owned_session(session_id, user)
    messages = _get_session_messages(session_id, user)

    # Validate target backend
    registry = get_backend_registry()
    target_spec = registry.get(req.target_backend)
    if not target_spec:
        raise HTTPException(status_code=400, detail=f"Unknown backend: {req.target_backend}")
    if not target_spec.available():
        raise HTTPException(status_code=400, detail=f"Backend {req.target_backend} not available")

    # Generate brief
    brief = generate_brief(session_id, messages, req.mode, user)

    # Create copyable prompt for the target agent
    copyable_prompt = f"Continue from session {session_id}.\n\n{brief}"
    if req.note:
        copyable_prompt += f"\n\nNote from user: {req.note}"

    # Create new session forked from original
    new_id = new_session_id()
    now = datetime.utcnow().isoformat() + "Z"

    new_session = {
        "id": new_id,
        "user_id": user["user_id"],
        "persona": s.get("persona", "build"),
        "project": s.get("project", "default"),
        "config": copy.deepcopy(s.get("config", {})),
        "messages": copy.deepcopy(messages) if req.mode == "full" else [],
        "cost_usd": 0.0,
        "created_at": now,
        "updated_at": now,
        "forked_from": session_id,
        "kind": "handoff",
        "handoff_target_backend": req.target_backend,
        "handoff_target_model": req.target_model,
        "handoff_mode": req.mode,
        "handoff_note": req.note,
        "handoff_delivery_state": "unconfirmed",
        "handoff_copyable_prompt": copyable_prompt,
    }
    _sessions[new_id] = new_session

    # Persist to history store with metadata
    from veya.history_store import default_history_store

    store = default_history_store()
    await store.save(
        new_id,
        new_session["messages"],
        user_id=user["user_id"],
        forked_from=session_id,
        kind="handoff",
        handoff_target_backend=req.target_backend,
        handoff_target_model=req.target_model,
        handoff_mode=req.mode,
        handoff_note=req.note,
        handoff_delivery_state="unconfirmed",
        handoff_copyable_prompt=copyable_prompt,
    )

    event_store.append(
        {
            "topic": "session.handoff",
            "session_id": new_id,
            "trace_id": new_id,
            "actor": user["user_id"],
            "payload": {
                "source_session": session_id,
                "target_backend": req.target_backend,
                "target_model": req.target_model,
                "mode": req.mode,
            },
        }
    )

    return {
        "session_id": new_id,
        "forked_from": session_id,
        "kind": "handoff",
        "target_backend": req.target_backend,
        "target_model": req.target_model,
        "mode": req.mode,
        "delivery_state": "unconfirmed",
        "copyable_prompt": copyable_prompt,
        "brief": brief,
    }


@router.get("/{session_id}/fork/preview")
async def fork_preview(
    session_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Preview the fork draft summary before confirming."""
    _owned_session(session_id, user)
    messages = _get_session_messages(session_id, user)
    brief = generate_brief(session_id, messages, "focused", user)
    return {
        "session_id": session_id,
        "draft_summary": brief,
    }


@router.post("/{session_id}/fork")
async def fork_session_polished(
    session_id: str,
    req: ForkPolishRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Fork session with editable draft summary (polish)."""
    s = _owned_session(session_id, user)
    messages = _get_session_messages(session_id, user)

    # Use provided summary or generate one
    summary = req.summary or generate_brief(session_id, messages, "focused", user)

    new_id = new_session_id()
    now = datetime.utcnow().isoformat() + "Z"

    new_session = {
        "id": new_id,
        "user_id": user["user_id"],
        "persona": s.get("persona", "build"),
        "project": s.get("project", "default"),
        "config": copy.deepcopy(s.get("config", {})),
        "messages": copy.deepcopy(messages),
        "cost_usd": 0.0,
        "created_at": now,
        "updated_at": now,
        "forked_from": session_id,
        "kind": "fork",
        "label": req.label,
    }
    _sessions[new_id] = new_session

    # Persist to history store
    from veya.history_store import default_history_store

    store = default_history_store()
    await store.save(
        new_id,
        new_session["messages"],
        user_id=user["user_id"],
        forked_from=session_id,
        kind="fork",
        worktree_path=s.get("cwd"),
    )

    event_store.append(
        {
            "topic": "session.fork",
            "session_id": new_id,
            "trace_id": new_id,
            "actor": user["user_id"],
            "payload": {
                "source_session": session_id,
                "label": req.label,
                "polished": req.polish,
            },
        }
    )

    return {
        "session_id": new_id,
        "forked_from": session_id,
        "kind": "fork",
        "label": req.label,
        "summary": summary,
    }
