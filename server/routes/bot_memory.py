"""REST endpoints for per-bot Markdown memory (P3 — Lorca-style memory).

Prefix: ``/api/v1/bots/{bot_id}/memory``

Auth: same pattern as ``server/routes/product.py`` — ``get_current_user``
(Bearer token → user dict, falls back to anonymous; no hard 401 gate so
existing callers without tokens are not broken).

Endpoints
---------
GET  /api/v1/bots/{bot_id}/memory
    List all ``*.md`` file names in the bot's memory directory.

GET  /api/v1/bots/{bot_id}/memory/{name}
    Read the content of one memory file.  Returns 404 if absent.

PUT  /api/v1/bots/{bot_id}/memory/{name}
    Write (create or overwrite) one memory file atomically.  Body is plain
    text (``Content-Type: text/plain``) or JSON ``{"text": "..."}`` for
    clients that prefer JSON.

DELETE /api/v1/bots/{bot_id}/memory/{name}
    Delete one memory file.  Returns 404 if absent.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.bot_team.memory import BotMemoryStore

logger = logging.getLogger("veya.bot_memory")

router = APIRouter(prefix="/api/v1/bots", tags=["bot-memory"])


# ── Request/Response models ──────────────────────────────────────────────────


class MemoryListResponse(BaseModel):
    bot_id: str
    files: list[str]


class MemoryReadResponse(BaseModel):
    bot_id: str
    name: str
    content: str


class MemoryWriteBody(BaseModel):
    """JSON body for PUT.  The plain-text path is handled separately."""

    text: str = Field(..., description="New file content")


class MemoryWriteResponse(BaseModel):
    bot_id: str
    name: str
    ok: bool = True


class MemoryDeleteResponse(BaseModel):
    bot_id: str
    name: str
    deleted: bool


# ── Helper ───────────────────────────────────────────────────────────────────


def _store(bot_id: str) -> BotMemoryStore:
    return BotMemoryStore(bot_id)


def _validate_or_422(name: str) -> None:
    """Raise HTTP 422 for unsafe or non-.md names (mirrors store validation)."""
    try:
        BotMemoryStore.__new__(BotMemoryStore)  # skip __init__
        # Re-use the store's validation logic via a throwaway instance.
        _store("__probe__")._path(name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get(
    "/{bot_id}/memory",
    response_model=MemoryListResponse,
    summary="List bot memory files",
)
async def list_memory(
    bot_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> MemoryListResponse:
    """Return sorted list of ``*.md`` file names in the bot's memory directory."""
    store = _store(bot_id)
    return MemoryListResponse(bot_id=bot_id, files=store.list_files())


@router.get(
    "/{bot_id}/memory/{name}",
    response_model=MemoryReadResponse,
    summary="Read one bot memory file",
)
async def read_memory(
    bot_id: str,
    name: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> MemoryReadResponse:
    """Return the content of *name*.  404 if the file does not exist."""
    _validate_or_422(name)
    store = _store(bot_id)
    content = store.read(name)
    if not content and name not in store.list_files():
        raise HTTPException(status_code=404, detail=f"memory file {name!r} not found")
    return MemoryReadResponse(bot_id=bot_id, name=name, content=content)


@router.put(
    "/{bot_id}/memory/{name}",
    response_model=MemoryWriteResponse,
    summary="Write (create or overwrite) one bot memory file",
)
async def write_memory(
    bot_id: str,
    name: str,
    request: Request,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> MemoryWriteResponse:
    """Write *name* atomically.

    Accepts either:
    - ``Content-Type: application/json`` body ``{"text": "..."}``
    - ``Content-Type: text/plain`` raw body
    """
    _validate_or_422(name)
    content_type = (request.headers.get("content-type") or "").lower()
    raw = await request.body()
    if "application/json" in content_type:
        import json as _json

        try:
            payload = _json.loads(raw)
            text = str(payload.get("text", ""))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON body: {exc}") from exc
    else:
        # Treat body as plain UTF-8 text.
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="body must be UTF-8") from exc

    store = _store(bot_id)
    try:
        store.write(name, text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return MemoryWriteResponse(bot_id=bot_id, name=name)


@router.delete(
    "/{bot_id}/memory/{name}",
    response_model=MemoryDeleteResponse,
    summary="Delete one bot memory file",
)
async def delete_memory(
    bot_id: str,
    name: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> MemoryDeleteResponse:
    """Delete *name*.  404 if it does not exist."""
    _validate_or_422(name)
    store = _store(bot_id)
    deleted = store.delete(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"memory file {name!r} not found")
    return MemoryDeleteResponse(bot_id=bot_id, name=name, deleted=True)


__all__ = ["router"]
