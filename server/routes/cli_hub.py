"""CLI-Hub REST surface (NANO-W8, ``VEYA_CLI_HUB``, default OFF).

Four endpoints over the CLI-Hub oprims:

* ``GET  /api/v1/cli-hub/search`` — read the CLI-Anything registry
* ``GET  /api/v1/cli-hub/skills/{skill_id}`` — read one entry's SKILL.md
* ``POST /api/v1/cli-hub/install`` — ask to install a pinned CLI **on a runner**
* ``POST /api/v1/cli-hub/invoke`` — ask to run an installed CLI on a runner

Two rules this surface enforces:

* **The server never installs anything.**  The published hub is the bot catalog
  and the conversations; a bot's runtime lives on the runner that owns it, so an
  install without a runner id is refused rather than quietly performed against
  the server's own environment.
* **Nothing is done implicitly.**  Install and any invocation go through the
  existing Action Gateway policy first, and a request the policy marks as
  needing approval is answered with a pending approval and no side effect.

The registry base URL is deployment configuration.  With
``VEYA_CLI_HUB_BASE_URL`` unset the read endpoints report that the registry is
not configured instead of reaching out to a default nobody chose.

CLI-Anything (HKUDS) is Apache-2.0 and is used as a protocol only.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.bot_team.runners import LOCAL_RUNNER_ID, RunnerError, RunnerRecord, RunnerRegistry
from server.feature_flags import enabled as flag_enabled
from server.privileged_action_gate import GateOutcome, request_privileged_action
from veya.obase.cli_hub import cli_hub_registry_base_url, cli_hub_registry_configured
from veya.oprim.cli_hub import (
    CLIHubUnsupportedError,
    read_cli_skill,
    search_cli,
)

logger = logging.getLogger("veya.cli_hub")

router = APIRouter(prefix="/api/v1/cli-hub", tags=["cli-hub"])


def _require_cli_hub() -> None:
    if not flag_enabled("VEYA_CLI_HUB"):
        raise HTTPException(
            status_code=404, detail="CLI Hub not enabled (VEYA_CLI_HUB=1)"
        )


def _registry_or_empty() -> str:
    """The registry this deployment chose, or ``""``.

    The read endpoints answer with an explicit empty result rather than
    reaching out to a default nobody configured: a silent call to a
    third-party index the deployment never named is worse than saying there is
    nothing to search yet.
    """

    return cli_hub_registry_base_url() if cli_hub_registry_configured() else ""


def _registry_unconfigured_payload() -> dict[str, Any]:
    return {
        "results": [],
        "available": False,
        "reason": (
            "no CLI-Hub registry is configured for this deployment "
            "(set VEYA_CLI_HUB_BASE_URL)"
        ),
    }


def _require_runner(runner_id: str) -> RunnerRecord:
    """Resolve a registered runner, refusing the local one as an install target.

    The P6 runner catalog is server-wide, so registration is the access check
    here; an unknown id answers 403 rather than 404 so the endpoint does not
    confirm the existence of a machine the caller may not see.
    """

    value = str(runner_id or "").strip()
    if not value or value == LOCAL_RUNNER_ID:
        raise HTTPException(
            status_code=403,
            detail=(
                "a CLI is installed on a runner, never on the Veya server; "
                "name a runner_id from the runner registry"
            ),
        )
    try:
        return RunnerRegistry().require(value)
    except RunnerError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _require_bot_owner(bot_id: str, user: dict[str, Any]) -> None:
    """Refuse an action naming a bot the caller does not own."""

    from server.goal_run.bot_identity import BotRegistry

    spec = BotRegistry().get(bot_id)
    if spec is None:
        return
    owner = str(spec.owner_id or "").strip()
    if owner and owner != str(user.get("user_id") or ""):
        raise HTTPException(status_code=403, detail=f"bot {bot_id!r} belongs to another user")


def _gate(**kwargs: Any) -> GateOutcome:
    """Ask the existing policy whether this side effect may run."""

    return request_privileged_action(**kwargs)


# ── request bodies ───────────────────────────────────────────────────────


class CLIInstallRequest(BaseModel):
    package: str = Field(..., min_length=1, max_length=128)
    version: str = Field(..., min_length=1, max_length=64)
    #: Required in practice: an install without one names the Veya server,
    #: which is refused rather than performed.
    runner_id: str = Field(default="", max_length=64)
    bot_id: str | None = Field(default=None, max_length=64)


class CLIInvokeRequest(BaseModel):
    command: list[str] = Field(..., min_length=1, max_length=16)
    runner_id: str = Field(default="", max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    bot_id: str | None = Field(default=None, max_length=64)


# ── endpoints ─────────────────────────────────────────────────────────────


@router.get("/search")
async def search_cli_route(
    q: str,
    current_user: dict = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Search the CLI-Hub registry for tools matching ``q``.

    Answers 200 either way: with no registry configured the payload says so
    explicitly rather than pretending there were no matches.
    """

    _require_cli_hub()
    base_url = _registry_or_empty()
    if not base_url:
        return {"query": q, **_registry_unconfigured_payload()}
    try:
        return search_cli(query=q, base_url=base_url)
    except CLIHubUnsupportedError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/skills/{skill_id}")
async def read_cli_skill_route(
    skill_id: str,
    current_user: dict = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Read one registry entry's SKILL.md."""

    _require_cli_hub()
    base_url = _registry_or_empty()
    if not base_url:
        raise HTTPException(status_code=503, detail=_registry_unconfigured_payload()["reason"])
    try:
        return read_cli_skill(skill_id=skill_id, base_url=base_url)
    except CLIHubUnsupportedError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/install")
async def install_cli_route(
    req: CLIInstallRequest,
    current_user: dict = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Request a pinned CLI install on a runner.

    The install itself happens on the runner, through the existing runner
    registry; this endpoint only decides whether it may be asked for.
    """

    _require_cli_hub()
    record = _require_runner(req.runner_id)
    if req.bot_id:
        _require_bot_owner(req.bot_id, current_user)

    outcome = _gate(
        action="cli_hub.install",
        arguments={
            "package": req.package,
            "version": req.version,
            "runner_id": record.runner_id,
            "bot_id": req.bot_id or "",
        },
        resource=record.runner_id,
        bot_id=req.bot_id or "",
        actor=str(current_user.get("user_id") or ""),
    )
    if not outcome.approved:
        raise HTTPException(
            status_code=outcome.http_status,
            detail=outcome.to_payload(approval_request_id=outcome.request_id),
        )
    # Approved here means the policy allows it; the physical install runs on the
    # runner through the runner client, never in this process.
    return outcome.to_payload(
        runner_id=record.runner_id,
        package=req.package,
        version=req.version,
        forwarded_to_runner=True,
        install_oprim="veya.oprim.cli_hub.install_cli_on_runner",
    )


@router.post("/invoke")
async def invoke_cli_route(
    req: CLIInvokeRequest,
    current_user: dict = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Request one CLI invocation on a runner.

    A write-capable invocation is the same class of action as an install, so it
    is gated the same way rather than executed from a request handler.
    """

    _require_cli_hub()
    record = _require_runner(req.runner_id)
    if req.bot_id:
        _require_bot_owner(req.bot_id, current_user)

    outcome = _gate(
        action="cli_hub.invoke",
        arguments={
            "command": req.command,
            "runner_id": record.runner_id,
            "bot_id": req.bot_id or "",
        },
        resource=record.runner_id,
        effect="process",
        bot_id=req.bot_id or "",
        actor=str(current_user.get("user_id") or ""),
    )
    if not outcome.approved:
        raise HTTPException(
            status_code=outcome.http_status,
            detail=outcome.to_payload(approval_request_id=outcome.request_id),
        )
    logger.info(
        "cli-hub invoke approved",
        extra={"runner_id": record.runner_id, "command": req.command[:1]},
    )
    return outcome.to_payload(
        runner_id=record.runner_id,
        command=req.command,
        invoke_oprim="veya.oprim.cli_hub.invoke_cli",
    )


__all__ = ["router"]
