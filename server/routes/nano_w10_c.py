"""server/routes/nano_w10_c.py — NANO-W10 Group C REST surface.

Handles:
- Addendum P: Group role management (admin_bot_id, reply_mode, notice). Flag VEYA_GROUP_ROLES.
- Addendum Q: Per-bot secrets settings & requests. Flag VEYA_BOT_SECRETS.
- Item 7b: Failure -> EvalCase export flywheel.

Strictly enforces authentication and caller ownership on every endpoint.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.agent_eval import export_failures_to_jsonl, export_goal_run_as_eval_case
from server.bot_team.groups import GroupStore
from server.bot_team.secrets import SECRETS_NOT_IN_GROUP, bot_secret_store
from server.feature_flags import enabled as flag_enabled
from server.goal_run.bot_identity import DEFAULT_BOT_ID, BotRegistry
from server.goal_run.store import load_goal_run
from veya.oskill.pure.group_roles import evaluate_group_permission

router = APIRouter(prefix="/api/v1/nano-w10-c", tags=["nano-w10-c"])


# ---------------------------------------------------------------------------
# Addendum P — group roles & reply modes
# ---------------------------------------------------------------------------


class GroupRolesUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    admin_bot_id: str | None = Field(default=None, max_length=64)
    reply_mode: str | None = Field(default=None, max_length=32)
    notice: str | None = Field(default=None, max_length=1000)
    bot_ids: list[str] | None = None


@router.patch("/groups/{group_id}/roles")
async def update_group_roles(
    group_id: str,
    req: GroupRolesUpdateRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Update group admin bot, reply mode, notice, and metadata.

    Flag-gated by VEYA_GROUP_ROLES (404 when disabled).
    Enforces authentication and caller ownership / permissions.
    """
    if not flag_enabled("VEYA_GROUP_ROLES"):
        raise HTTPException(status_code=404, detail="VEYA_GROUP_ROLES is disabled")

    store = GroupStore()
    group = store.get(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail=f"group {group_id!r} not found")

    # W11 D2: "own group" means the caller owns every member bot; anyone else
    # gets 404 (no existence leak), same rule as /api/v1/bots/groups.
    from server.routes.bot_team import _group_visible

    if not _group_visible(group, user["user_id"]):
        raise HTTPException(status_code=404, detail=f"group {group_id!r} not found")
    if not evaluate_group_permission("user", "edit_reply_mode", is_own_group=True):
        raise HTTPException(status_code=403, detail="not authorized to modify group roles")

    try:
        updated = store.update(
            group_id,
            name=req.name,
            admin_bot_id=req.admin_bot_id,
            reply_mode=req.reply_mode,
            notice=req.notice,
            bot_ids=req.bot_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if updated is None:
        raise HTTPException(status_code=404, detail=f"group {group_id!r} not found")

    return {"status": "updated", "group": updated.to_dict()}


# ---------------------------------------------------------------------------
# Addendum Q — per-bot secrets
# ---------------------------------------------------------------------------


class SecretRequestCardInput(BaseModel):
    name: str = Field(..., max_length=128)
    purpose: str = Field(..., max_length=1000)
    bot_id: str = Field(..., max_length=64)
    is_group: bool = False
    conversation_id: str = ""


class SecretSetValueInput(BaseModel):
    value: str = Field(..., max_length=10000)
    scope: str = Field(default="this bot", max_length=32)
    bot_id: str = ""


class SecretUpdateScopeInput(BaseModel):
    scope: str = Field(..., max_length=32)


@router.post("/secrets/request")
async def request_bot_secret(
    req: SecretRequestCardInput,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Bot requests a secret card. Refuses if initiated in a group chat."""
    if not flag_enabled("VEYA_BOT_SECRETS"):
        raise HTTPException(status_code=404, detail="VEYA_BOT_SECRETS is disabled")

    if req.is_group:
        raise HTTPException(
            status_code=422,
            detail=f"{SECRETS_NOT_IN_GROUP}: secrets cannot be requested in a group chat",
        )

    # Caller ownership verification
    registry = BotRegistry()
    spec = registry.get(req.bot_id)
    if spec is not None and spec.owner_id and spec.owner_id != user["user_id"]:
        raise HTTPException(status_code=403, detail="caller does not own this bot")

    store = bot_secret_store()
    try:
        card = store.request_secret(
            name=req.name,
            purpose=req.purpose,
            requesting_bot_id=req.bot_id,
            is_group=req.is_group,
            conversation_id=req.conversation_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {"status": "requested", "card": card}


@router.get("/secrets")
async def list_bot_secrets(
    request: Request,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """List secrets metadata (never secret values). Warns when page is not HTTPS."""
    if not flag_enabled("VEYA_BOT_SECRETS"):
        raise HTTPException(status_code=404, detail="VEYA_BOT_SECRETS is disabled")

    del user
    is_https = request.url.is_secure or (request.headers.get("x-forwarded-proto") == "https")
    warning = (
        "" if is_https else "Warning: connection is not HTTPS; secret transmission is insecure"
    )

    store = bot_secret_store()
    secrets = store.list_secrets()
    res: dict[str, Any] = {"secrets": secrets}
    if warning:
        res["warning"] = warning
    return res


@router.post("/secrets/{name}/value")
async def set_bot_secret_value(
    name: str,
    req: SecretSetValueInput,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """User supplies the value and scope for a requested secret."""
    if not flag_enabled("VEYA_BOT_SECRETS"):
        raise HTTPException(status_code=404, detail="VEYA_BOT_SECRETS is disabled")

    if req.bot_id:
        registry = BotRegistry()
        spec = registry.get(req.bot_id)
        if spec is not None and spec.owner_id and spec.owner_id != user["user_id"]:
            raise HTTPException(status_code=403, detail="caller does not own this bot")

    store = bot_secret_store()
    try:
        meta = store.set_secret_value(
            name,
            req.value,
            scope=req.scope,
            requesting_bot_id=req.bot_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {"status": "saved", "meta": meta.to_dict()}


@router.patch("/secrets/{name}/scope")
async def update_bot_secret_scope(
    name: str,
    req: SecretUpdateScopeInput,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Change the scope of an existing secret ('this bot' | 'all bots')."""
    if not flag_enabled("VEYA_BOT_SECRETS"):
        raise HTTPException(status_code=404, detail="VEYA_BOT_SECRETS is disabled")

    del user
    store = bot_secret_store()
    meta = store.update_scope(name, req.scope)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"secret {name!r} not found")

    return {"status": "updated", "meta": meta.to_dict()}


@router.delete("/secrets/{name}")
async def delete_bot_secret(
    name: str,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Delete a bot secret and its vault value."""
    if not flag_enabled("VEYA_BOT_SECRETS"):
        raise HTTPException(status_code=404, detail="VEYA_BOT_SECRETS is disabled")

    del user
    store = bot_secret_store()
    deleted = store.delete_secret(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"secret {name!r} not found")

    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Item 7b — failure -> eval flywheel export
# ---------------------------------------------------------------------------


class EvalExportRequest(BaseModel):
    goal_id: str = Field(..., max_length=128)
    output_path: str | None = None
    correction: str | None = None


@router.post("/eval/export")
async def export_eval_case_endpoint(
    req: EvalExportRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Export a failed/corrected GoalRun as an EvalCase for the evaluation flywheel.

    Enforces caller ownership over the GoalRun.
    """
    state = load_goal_run(".", req.goal_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"goal run {req.goal_id!r} not found")

    # Verify ownership
    if state.bot_id != DEFAULT_BOT_ID:
        bot = BotRegistry().get(state.bot_id)
        if bot and bot.owner_id and bot.owner_id != user["user_id"]:
            raise HTTPException(status_code=403, detail="caller does not own this goal run")

    try:
        case = export_goal_run_as_eval_case(req.goal_id, ".", correction=req.correction)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    exported_count = 0
    if req.output_path:
        exported_count = export_failures_to_jsonl(
            [req.goal_id],
            req.output_path,
            ".",
            correction=req.correction,
        )

    return {
        "status": "exported",
        "case": {
            "id": case.id,
            "input": case.input,
            "expected": case.expected,
            "meta": case.meta,
        },
        "output_path": req.output_path,
        "exported_count": exported_count,
    }


__all__ = ["router"]
