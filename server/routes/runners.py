"""Lorca P6 REST: the machines a bot may run on, and a turn for a remote hub.

Two surfaces live here, on purpose:

* ``/api/v1/runners`` — managing the runner catalog (list / add / remove /
  health check / bind a bot to a runner) and probing a runner with a timeout.
  Auth is the same ``Depends(auth_mod.get_current_user)`` the bot endpoints
  use.
* ``POST /api/v1/runners/turn`` — the endpoint a *remote hub* calls when one of
  its bots runs on this machine. It is authenticated by a bearer token verified
  through the existing :class:`~veya.remote.auth.RemoteAuth` (fail closed, no
  new auth scheme), and it executes the turn locally through the normal
  ``MasterAgent → GoalRun`` path with this server's own GoalRun and task
  projection.

No authority is added here: the hub keeps the catalog and the conversations,
the runner owns execution, and this file only moves a turn between them.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.bot_team.runner_client import default_runner_client
from server.bot_team.runners import (
    LOCAL_RUNNER_ID,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    RunnerError,
    local_runner_view,
    runner_registry,
)
from server.goal_run.bot_identity import BotRegistry, BotSpec

router = APIRouter(prefix="/api/v1/runners", tags=["runners"])


# ── request bodies ───────────────────────────────────────────────────────


class RunnerCreateRequest(BaseModel):
    runner_id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(default="", max_length=120)
    #: Tailnet address of the other Veya server, e.g. ``http://100.64.0.5:8765``.
    base_url: str = Field(..., min_length=1, max_length=512)
    #: Env var name or file path holding the runner's secret. Never the secret.
    token_ref: str = Field(..., min_length=1, max_length=512)


class RunnerTurnBot(BaseModel):
    """The bot as the hub knows it (the hub owns the catalog)."""

    bot_id: str = Field(..., min_length=1, max_length=64)
    name: str = ""
    description: str = ""
    instructions: str = ""
    provider: str = ""
    model: str = ""
    owner_id: str = ""


class RunnerTurnRequest(BaseModel):
    version: int = 1
    runner_id: str = ""
    trace_id: str = ""
    user: dict[str, Any] = Field(default_factory=dict)
    workspace_id: str | None = None
    bot: RunnerTurnBot
    text: str = Field(..., min_length=1, max_length=20_000)
    context: str = ""


# ── the runner-facing turn endpoint (declared first so the static ``/turn``
# ── segment wins over ``/{runner_id}``, the same trick bots.py uses for
# ── ``/groups``).


def runner_auth() -> Any:
    """The credential authority of this runner, rebuilt per request.

    Reusing :class:`~veya.remote.auth.RemoteAuth` means a runner token is
    issued, rotated and revoked with the tooling that already exists, and a
    token the operator just changed takes effect on the next turn.
    """

    from veya.remote.auth import RemoteAuth, RemoteAuthError

    try:
        return RemoteAuth.from_env()
    except Exception as exc:  # a broken store must deny, never admit
        raise RemoteAuthError("runner access is not configured") from exc


@router.post("/turn")
async def run_runner_turn(
    req: RunnerTurnRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Execute one turn of a remote hub's bot on this machine.

    The turn goes through the same product path a local turn goes through, so
    the durable record (GoalRun + the task projection) is this machine's.
    """

    from veya.remote.auth import RemoteAuthError

    try:
        token = runner_auth().verify(authorization)
    except RemoteAuthError as exc:
        raise HTTPException(
            status_code=401, detail=str(exc.message or "authentication denied")
        ) from exc

    # Fail closed on the grant: causing a bot turn to run real work needs
    # ``write`` on the presenting token (``RemotePermissions`` defaults to
    # read-only, so an unconfigured issue is refused here).
    if not token.permissions.write:
        raise HTTPException(
            status_code=403,
            detail="this runner token may not execute turns (requires the write permission)",
        )

    user_id = str(req.user.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=422, detail="user.user_id is required")

    # Imported here so the module stays importable without the product stores.
    from server.bot_team.turns import run_bot_turn

    # ``runner_id`` is forced to "local": executing a forwarded turn must never
    # forward again, or two hubs could bounce one turn between them.
    spec = BotSpec(
        bot_id=req.bot.bot_id,
        owner_id=req.bot.owner_id or user_id,
        name=req.bot.name,
        description=req.bot.description,
        instructions=req.bot.instructions,
        provider=req.bot.provider,
        model=req.bot.model,
        runner_id=LOCAL_RUNNER_ID,
    )
    try:
        turn = await run_bot_turn(
            bot=spec,
            text=req.text,
            user={"user_id": user_id, "username": str(req.user.get("username") or user_id)},
            context=req.context,
            workspace_id=req.workspace_id,
            trace_id=req.trace_id,
        )
    except Exception as exc:  # one bad turn must not take the runner down
        raise HTTPException(
            status_code=502, detail=f"runner could not execute the turn: {exc}"
        ) from exc
    return {"ok": True, "turn": turn.to_dict()}


# ── runner management ────────────────────────────────────────────────────


@router.get("")
async def list_runners(
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Every runner. The implicit ``local`` runner is always first."""

    del user
    return {
        "runners": runner_registry().views(),
        "default_runner_id": LOCAL_RUNNER_ID,
    }


@router.post("")
async def add_runner(
    req: RunnerCreateRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Register one runner machine. The secret is only ever a reference."""

    del user
    try:
        record = runner_registry().add(
            runner_id=req.runner_id,
            name=req.name,
            base_url=req.base_url,
            token_ref=req.token_ref,
        )
    except RunnerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="runner registry is not writable") from exc
    return {"status": "created", "runner": record.to_view()}


@router.post("/{runner_id}/health")
async def check_runner_health(
    runner_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """``GET {base_url}/health`` on the runner, with a timeout.

    A runner that does not answer keeps its record and is marked offline; it is
    never forgotten, because forgetting it would silently send its bots home.
    """

    del user
    if runner_id == LOCAL_RUNNER_ID:
        return {"status": "local", "runner": local_runner_view()}
    registry = runner_registry()
    try:
        record = registry.require(runner_id)
    except RunnerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    probe = await default_runner_client().health(record)
    status = STATUS_ONLINE if probe.get("status") == STATUS_ONLINE else STATUS_OFFLINE
    try:
        updated = registry.record_health(runner_id, status=status)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="runner registry is not writable") from exc
    return {
        "status": "checked",
        "runner": (updated or record).to_view(),
        "health": probe,
    }


@router.post("/{runner_id}/bots/{bot_id}")
async def set_bot_runner(
    runner_id: str,
    bot_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Point one bot at a runner (``local`` brings it back to this server)."""

    del user
    bots = BotRegistry()
    spec = bots.get(bot_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"bot {bot_id!r} not found")
    if runner_id == LOCAL_RUNNER_ID:
        target, base_url = LOCAL_RUNNER_ID, ""
    else:
        try:
            record = runner_registry().require(runner_id)
        except RunnerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        target, base_url = record.runner_id, record.base_url
    try:
        bots.register(replace(spec, runner_id=target))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="bot registry is not writable") from exc
    return {
        "status": "updated",
        "bot_id": spec.bot_id,
        "runner_id": target,
        "runner_base_url": base_url,
    }


@router.delete("/{runner_id}")
async def remove_runner(
    runner_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Forget one runner. Bots still pointing at it fail their turns visibly."""

    del user
    if runner_id == LOCAL_RUNNER_ID:
        raise HTTPException(status_code=422, detail="the local runner is this server")
    try:
        removed = runner_registry().remove(runner_id)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="runner registry is not writable") from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"runner {runner_id!r} not found")
    return {"status": "deleted", "runner_id": runner_id}


__all__ = ["router"]
