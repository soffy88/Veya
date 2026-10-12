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
from server.acp_mcp_delivery import McpAttachPolicy
from server.mcp_presets import attach_preset, preset_statuses

router = APIRouter(prefix="/api/v1/mcp-presets", tags=["mcp-presets"])


class AttachRequest(BaseModel):
    bot_id: str | None = None


@router.get("")
async def list_presets() -> dict[str, Any]:
    """Read-only public view of the 5 preset definitions + availability.

    No authentication (read-only metadata, no secret values). The
    response only lists preset names + required env var *names* (like
    ``BRAVE_API_KEY``), never env values.
    """
    statuses = preset_statuses()
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


def _owned_bot(bot_id: str, user: dict[str, Any]) -> str:
    """Return bot_id if it belongs to the caller, else 404 (no existence leak).

    Preset attachment is session-scoped, but callers must already own
    the requesting bot before toggling external MCP capabilities.
    """
    from server.goal_run.bot_identity import BotRegistry

    spec = BotRegistry().get(bot_id)
    if spec is None or str(getattr(spec, "owner_id", "") or "") != str(
        user.get("user_id") or ""
    ):
        raise HTTPException(status_code=404, detail=f"bot {bot_id!r} not found")
    return bot_id


@router.post("/{name}/attach")
async def attach_one_preset(
    name: str,
    req: AttachRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Attach one preset through the EXISTING McpAttachPolicy.

    Caller must authenticate (require_user) and own the requesting bot
    (owner_id check, 404 otherwise). Flag OFF → 404.
    Missing KEY → 409 with "unavailable" reason.
    """
    _owned_bot(req.bot_id or "", user)

    statuses = preset_statuses()
    target = next((s for s in statuses if s["name"] == name), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"preset {name!r} not found")

    if not target["available"]:
        raise HTTPException(
            status_code=409,
            detail=f"MCP preset '{name}' unavailable: {target['reason']}",
        )

    from server.mcp_presets import _load_presets

    preset = next(p for p in _load_presets() if p["name"] == name)
    result = await attach_preset(preset, policy=McpAttachPolicy())
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
