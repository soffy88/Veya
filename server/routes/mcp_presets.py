"""server/routes/mcp_presets — W14 MCP preset pack REST surface.

Thin read-only query + authenticated attach endpoint over the EXISTING
MCP path (server.mcp_presets module). No new registry/scheduler/approval
authority is created; attach() delegates to the same
``McpAttachPolicy`` + ``AcpMcpDelivery`` path used everywhere else.

list() is public (metadata only — names + required env var names, no
secret values). attach() requires authentication + bot ownership.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import auth as auth_mod
from server import mcp_presets
from server.acp_mcp_delivery import McpAttachPolicy

router = APIRouter(prefix="/api/v1/mcp-presets", tags=["mcp-presets"])


class AttachRequest(BaseModel):
    bot_id: str | None = None


def _require_user() -> dict[str, Any]:
    """Auth dependency indirection for test monkeypatching.

    FastAPI binds ``Depends(fn)`` at import time, so monkeypatching
    ``auth_mod.require_user`` after import would not take effect on an
    already-resolved dependency. This wrapper looks the attribute up at
    call time so tests can override the auth dependency through
    ``auth_mod.require_user``. Tries the real 1-arg signature first
    (explicit ``None`` so the Header default isn't passed through),
    falling back to 0 args for zero-arg test mocks.
    """
    try:
        return auth_mod.require_user(None)
    except TypeError:
        return auth_mod.require_user()


@router.get("")
async def list_presets() -> dict[str, Any]:
    """Read-only public view of the 5 preset definitions + availability."""
    statuses = mcp_presets.preset_statuses()
    return {
        "presets": [
            {
                "name": s["name"],
                "available": s["available"],
                "reason": s["reason"],
            }
            for s in statuses
        ],
    }


@router.post("/{name}/attach")
async def attach_one_preset(
    name: str,
    req: AttachRequest,
    user: dict[str, Any] = Depends(_require_user),
) -> dict[str, Any]:
    """Attach one preset through the EXISTING McpAttachPolicy."""
    # Defer lookup so tests can monkeypatch mcp_presets._owned_bot
    mcp_presets._owned_bot(req.bot_id or "", user)

    statuses = mcp_presets.preset_statuses()
    target = next((s for s in statuses if s["name"] == name), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"preset {name!r} not found")

    if not target["available"]:
        raise HTTPException(
            status_code=409,
            detail=f"MCP preset '{name}' unavailable: {target['reason']}",
        )

    preset = next(p for p in mcp_presets._load_presets() if p["name"] == name)
    result = await mcp_presets.attach_preset(preset, policy=McpAttachPolicy())
    if result.get("attached"):
        return {"status": "attached", "preset": name}
    raise HTTPException(
        status_code=503,
        detail=(
            f"MCP preset '{name}' could not be attached: "
            f"{result.get('status', result.get('reason', 'unknown'))}"
        ),
    )


__all__ = ["router"]
