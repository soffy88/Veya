"""NANO-W6 addendum J: device pairing REST (thin layer over the omodul flow).

A new device (runner or browser) requests a short pairing code; a signed-in
user confirms that code in Veya (UI + API); the device receives a credential
and — for runners — is registered in the existing runner registry. The whole
flow runs through ``veya/omodul/pairing_flow.py`` (decision_trail pillar);
codes and credentials are never logged.

Flag ``VEYA_DEVICE_PAIRING`` (default OFF): when off the endpoints 404 and
behaviour is unchanged.
"""

from __future__ import annotations

import os
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError

from server import auth as auth_mod
from server.feature_flags import enabled as flag_enabled
from veya.obase.pairing import PairingCodeStore

FLAG_DEVICE_PAIRING = "VEYA_DEVICE_PAIRING"

router = APIRouter(prefix="/api/v1/pairing", tags=["pairing"])

_RUNNER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def device_pairing_enabled() -> bool:
    try:
        return flag_enabled(FLAG_DEVICE_PAIRING)
    except KeyError:
        return False


def _store() -> PairingCodeStore:
    return PairingCodeStore()


def _issue_credential(device_kind: str, device_label: str) -> dict[str, Any]:
    """Issue a device credential (raw token returned once; digest stored)."""

    return _store().issue_credential(device_kind=device_kind, device_label=device_label)


def _register_device(
    device_kind: str, device_label: str, credential: dict[str, Any]
) -> dict[str, Any]:
    """Register a paired device in the existing runner/device registry.

    The registry stores a *reference* to the credential's secret (an env-var
    name), never the secret — the same convention the P6 runner registry
    already uses.
    """

    if device_kind != "runner":
        return {"registered": False, "reason": "browser devices hold their credential only"}
    from server.bot_team.runners import RunnerError, runner_registry

    runner_id = str(device_label or "").strip()
    if not _RUNNER_ID_RE.match(runner_id):
        return {"registered": False, "reason": "device_label must be a valid runner id"}
    token_id = str((credential or {}).get("token_id") or "")
    base_url = str(os.environ.get("VEYA_PAIRED_RUNNER_BASE_URL") or "").strip()
    try:
        added = runner_registry().add(
            runner_id=runner_id,
            name=runner_id,
            base_url=base_url or "http://127.0.0.1:8765",
            token_ref=f"VEYA_DEVICE_TOKEN_{token_id.upper()}",
        )
        return {"registered": True, "runner": added.to_view()}
    except RunnerError as exc:
        return {"registered": False, "reason": str(exc)}


class PairingRequest(BaseModel):
    device_kind: str = Field(default="browser", pattern="^(runner|browser)$")
    device_label: str = Field(default="", max_length=120)
    #: For runner devices: the tailnet address the hub should forward to.
    base_url: str = Field(default="", max_length=512)


class PairingConfirm(BaseModel):
    code: str = Field(..., min_length=4, max_length=64)
    device_label: str = Field(default="", max_length=120)


@router.post("/request")
async def pairing_request(body: PairingRequest) -> dict[str, Any]:
    """Device side: request a short pairing code (rate-limited, TTL, single use)."""

    if not device_pairing_enabled():
        raise HTTPException(status_code=404, detail="device pairing is disabled")
    from veya.omodul.pairing_flow import PairingFlowConfig, run_pairing_flow

    result = await run_pairing_flow(
        PairingFlowConfig(store=_store()),
        {"op": "request", **body.model_dump()},
        "/tmp/veya-pairing",
    )
    if result.get("status") != "completed":
        raise HTTPException(status_code=429, detail=str(result.get("error") or "request refused"))
    return result


@router.post("/confirm")
async def pairing_confirm(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """User side: confirm a pairing code → credential issued + device registered."""

    # flag check first, so a disabled feature is a plain 404 (not a 422)
    if not device_pairing_enabled():
        raise HTTPException(status_code=404, detail="device pairing is disabled")
    try:
        body = PairingConfirm.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    from veya.omodul.pairing_flow import PairingFlowConfig, run_pairing_flow

    result = await run_pairing_flow(
        PairingFlowConfig(
            store=_store(),
            issue_credential=_issue_credential,
            register_device=_register_device,
        ),
        {"op": "confirm", "code": body.code, "device_label": body.device_label},
        "/tmp/veya-pairing",
    )
    if result.get("status") != "completed":
        raise HTTPException(status_code=400, detail=str(result.get("error") or "invalid code"))
    return result


__all__ = ["FLAG_DEVICE_PAIRING", "device_pairing_enabled", "router"]
