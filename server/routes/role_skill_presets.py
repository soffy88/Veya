"""server/routes/role_skill_presets — W15 role & skill preset REST surface.

* ``GET  /api/v1/presets/roles``   — list role templates (``available:false`` while flag off)
* ``GET  /api/v1/presets/skills``  — list skill presets
* ``POST /api/v1/bots/{bot_id}/presets/apply`` — auth + strict owner check, then
  writes through the ONE bot catalog (``BotRegistry``) — no second store.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server import role_skill_presets as presets

router = APIRouter(tags=["presets"])


class ApplyRequest(BaseModel):
    role_id: str | None = Field(default=None, max_length=64)
    skill_ids: list[str] | None = Field(default=None, max_length=50)


def _require_user() -> dict[str, Any]:
    """Call-time lookup so tests can monkeypatch ``auth_mod.require_user``."""
    try:
        return auth_mod.require_user(None)
    except TypeError:
        return auth_mod.require_user()


def _registry() -> Any:
    from server.routes.bot_team import bot_registry

    return bot_registry()


@router.get("/api/v1/presets/roles")
async def list_roles() -> dict[str, Any]:
    return presets.listing("role")


@router.get("/api/v1/presets/skills")
async def list_skills() -> dict[str, Any]:
    return presets.listing("skill")


@router.post("/api/v1/bots/{bot_id}/presets/apply")
async def apply_presets(
    bot_id: str,
    req: ApplyRequest,
    user: dict[str, Any] = Depends(_require_user),
) -> dict[str, Any]:
    if not presets.flag_enabled():
        raise HTTPException(status_code=409, detail=f"{presets.FLAG_NAME} is off")
    registry = _registry()
    spec = registry.get(bot_id)
    owner = str(user.get("user_id") or "")
    # Strict ownership: only a catalog bot the caller owns. Shared/system bots
    # (default, Chef, ownerless) are not modified through presets; foreign
    # bots answer 404 so their existence does not leak.
    if spec is None or not owner or str(getattr(spec, "owner_id", "") or "") != owner:
        raise HTTPException(status_code=404, detail=f"bot {bot_id!r} not found")
    try:
        updated, summary = presets.apply_to_spec(
            spec, role_id=req.role_id, skill_ids=req.skill_ids
        )
    except presets.PresetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        registry.register(updated)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="bot registry is not writable") from exc
    from server.routes.bot_team import _bot_view

    return {"status": "applied", **summary, "bot": _bot_view(updated)}
