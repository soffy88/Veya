"""Runner self-update and login-service REST surface (NANO-W8).

Four endpoints, all behind ``VEYA_RUNNER_SELFUPDATE`` (default OFF):

* ``GET  /api/v1/runner/self-update/check`` — is a newer runner published?
* ``POST /api/v1/runner/self-update/request`` — ask a runner to update
* ``POST /api/v1/runner/service/install`` — plan (or write) the login unit
* ``POST /api/v1/runner/service/uninstall`` — remove it
* ``GET  /api/v1/runner/service/status`` — is it installed and active

Three rules this surface enforces:

* **The server does not update itself.**  A runner checks the manifest and
  restarts itself; this endpoint asks a *named, registered* runner to do that,
  and refuses the local runner, which is this server.
* **Nothing happens without approval.**  The update request and a real (non
  dry-run) service write are gated by the existing Action Gateway policy; a
  request that needs approval is answered 202 with a pending request id and no
  side effect.
* **Service writes are dry by default.**  ``dry_run`` defaults to true, so the
  endpoint returns the unit it *would* write and writes nothing until a caller
  explicitly asks otherwise (and is approved).

The release manifest endpoint is deployment configuration.  With
``VEYA_RUNNER_RELEASE_MANIFEST_URL`` unset the check endpoint reports that
rather than reaching out to a default nobody chose.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.bot_team.runners import LOCAL_RUNNER_ID, RunnerError, RunnerRegistry
from server.feature_flags import enabled as flag_enabled
from server.privileged_action_gate import request_privileged_action
from veya.obase.runner_release import MANIFEST_URL_ENV
from veya.oprim.runner_ops import (
    RunnerOpsError,
    runner_self_update_check,
    runner_service_install,
    runner_service_status,
    runner_service_uninstall,
)

router = APIRouter(prefix="/api/v1/runner", tags=["runner-ops"])


def _require_self_update() -> None:
    if not flag_enabled("VEYA_RUNNER_SELFUPDATE"):
        raise HTTPException(
            status_code=404,
            detail="runner self-update not enabled (VEYA_RUNNER_SELFUPDATE=1)",
        )


def _require_remote_runner(runner_id: str) -> str:
    """Resolve a registered runner. The local runner is this server."""

    value = str(runner_id or "").strip()
    if not value:
        raise HTTPException(status_code=422, detail="runner_id is required")
    if value == LOCAL_RUNNER_ID:
        raise HTTPException(
            status_code=403,
            detail=(
                "the local runner is this Veya server; a runner updates itself, "
                "it is not updated through this API"
            ),
        )
    try:
        return RunnerRegistry().require(value).runner_id
    except RunnerError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


class SelfUpdateRequest(BaseModel):
    runner_id: str = Field(..., min_length=1, max_length=64)
    current_version: str = Field(default="", max_length=64)


class ServiceInstallRequest(BaseModel):
    runner_binary: str = Field(..., min_length=1, max_length=512)
    user: str | None = Field(default=None, max_length=64)
    working_dir: str | None = Field(default=None, max_length=512)
    #: Dry by default: the endpoint shows the unit it would write and writes
    #: nothing until a caller explicitly asks for the real thing.
    dry_run: bool = True


class ServiceUninstallRequest(BaseModel):
    user: str | None = Field(default=None, max_length=64)


@router.get("/self-update/check")
async def check_runner_update(
    current_version: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Whether a newer runner release is published.

    Answers 200 either way: with no manifest configured for this deployment the
    payload says so rather than calling out to a default endpoint.
    """

    _require_self_update()
    manifest_url = os.environ.get(MANIFEST_URL_ENV, "").strip()
    if not manifest_url:
        return {
            "update_available": False,
            "current_version": current_version,
            "latest_version": "",
            "configured": False,
            "reason": (
                f"no release manifest is configured for this deployment (set {MANIFEST_URL_ENV})"
            ),
        }
    try:
        result = runner_self_update_check(
            current_version=current_version, manifest_url=manifest_url
        )
    except RunnerOpsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {**result, "configured": True}


@router.post("/self-update/request")
async def request_runner_update(
    req: SelfUpdateRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Ask a named runner to update itself.

    The runner performs the whole sequence — check, signed download, stage,
    restart — on its own machine; this endpoint decides whether it may be
    asked, and performs nothing itself.
    """

    _require_self_update()
    runner_id = _require_remote_runner(req.runner_id)

    outcome = request_privileged_action(
        action="runner.self_update.request",
        arguments={"runner_id": runner_id, "current_version": req.current_version},
        resource=runner_id,
        bot_id="",
        actor=str(user.get("user_id") or ""),
    )
    if not outcome.approved:
        raise HTTPException(
            status_code=outcome.http_status,
            detail=outcome.to_payload(approval_request_id=outcome.request_id),
        )
    return outcome.to_payload(
        runner_id=runner_id,
        forwarded_to_runner=True,
        runner_decides_restart_eligibility="veya.oskill.runner_ops.runner_safe_to_restart",
    )


@router.post("/service/install")
async def install_runner_service(
    req: ServiceInstallRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Plan (by default) or write the unit that starts this runner at login."""

    _require_self_update()
    if not req.dry_run:
        outcome = request_privileged_action(
            action="runner.service.install",
            arguments={
                "runner_binary": req.runner_binary,
                "user": req.user or "",
                "working_dir": req.working_dir or "",
            },
            resource=req.runner_binary,
            effect="local_write",
            actor=str(user.get("user_id") or ""),
        )
        if not outcome.approved:
            raise HTTPException(
                status_code=outcome.http_status,
                detail=outcome.to_payload(approval_request_id=outcome.request_id),
            )

    result = runner_service_install(
        runner_binary=req.runner_binary,
        user=req.user,
        working_dir=req.working_dir,
        dry_run=req.dry_run,
    )
    if not result.get("success"):
        raise HTTPException(
            status_code=500, detail=result.get("error") or "the unit could not be written"
        )
    return result


@router.post("/service/uninstall")
async def uninstall_runner_service(
    req: ServiceUninstallRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Remove the login unit."""

    _require_self_update()
    outcome = request_privileged_action(
        action="runner.service.uninstall",
        arguments={"user": req.user or ""},
        resource="runner-service",
        effect="local_write",
        actor=str(user.get("user_id") or ""),
    )
    if not outcome.approved:
        raise HTTPException(
            status_code=outcome.http_status,
            detail=outcome.to_payload(approval_request_id=outcome.request_id),
        )
    return runner_service_uninstall(user=req.user)


@router.get("/service/status")
async def runner_service_status_endpoint(
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Whether the login unit is installed and active on this machine."""

    _require_self_update()
    return runner_service_status()


__all__ = ["router"]
