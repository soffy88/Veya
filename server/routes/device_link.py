"""Device-link routes: devices, presence, cross-device scopes, envelopes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import auth as auth_mod
from server.device_link import DeviceLinkStore, build_envelope

router = APIRouter(prefix="/api/v1/device-link", tags=["device-link"])


def _store() -> DeviceLinkStore:
    return DeviceLinkStore()


class DeviceCreateRequest(BaseModel):
    device_id: str
    label: str = ""
    platform: str = ""


class ScopeCreateRequest(BaseModel):
    host_device: str
    guest_device: str
    ref: str = ""


class EnvelopeRequest(BaseModel):
    kind: str
    src: str
    dst: str
    payload: Any = None


def _http_from_store_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.post("/devices")
async def register_device(
    req: DeviceCreateRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    store = _store()
    if store.get(req.device_id) is not None:
        raise HTTPException(status_code=409, detail=f"device {req.device_id!r} already exists")
    try:
        device = store.register_device(
            req.device_id, user["user_id"], label=req.label, platform=req.platform
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "created", "device": device.to_dict()}


@router.post("/devices/{device_id}/touch")
async def touch_device(
    device_id: str,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    try:
        device = _store().touch(device_id, user["user_id"])
    except (KeyError, PermissionError) as exc:
        raise _http_from_store_error(exc) from exc
    return {"status": "touched", "device": device.to_dict()}


@router.get("/devices")
async def list_devices(
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    return {"devices": [d.to_dict() for d in _store().list_devices(user["user_id"])]}


@router.post("/provider-shares")
async def create_provider_share(
    req: ScopeCreateRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    return _create_scope("provider-share", req, user)


@router.post("/shared-tasks")
async def create_shared_task(
    req: ScopeCreateRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    return _create_scope("shared-task", req, user)


def _create_scope(
    kind: str, req: ScopeCreateRequest, user: dict[str, Any]
) -> dict[str, Any]:
    try:
        scope = _store().create_scope(
            kind,
            user["user_id"],
            host_device=req.host_device,
            guest_device=req.guest_device,
            ref=req.ref,
        )
    except (KeyError, PermissionError, ValueError) as exc:
        raise _http_from_store_error(exc) from exc
    return {"status": "created", "scope": scope.to_dict()}


@router.get("/scopes")
async def list_scopes(
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    return {"scopes": [s.to_dict() for s in _store().list_scopes(user["user_id"])]}


@router.post("/scopes/{scope_id}/revoke")
async def revoke_scope(
    scope_id: str,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    try:
        ok = _store().revoke_scope(scope_id, user["user_id"])
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"unknown scope: {scope_id!r}")
    return {"status": "revoked"}


@router.post("/envelopes")
async def build_relay_envelope(
    req: EnvelopeRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Build a relay envelope. src and dst must both be the caller's devices.

    The relay authors the routing header from ownership, never from a bare
    client claim: an unknown device is 404, someone else's device is 403.
    """

    store = _store()
    for device_id in (req.src, req.dst):
        try:
            store.require_owned(device_id, user["user_id"])
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        envelope = build_envelope(kind=req.kind, src=req.src, dst=req.dst, payload=req.payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"envelope": envelope}
