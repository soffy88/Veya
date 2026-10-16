from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from server.auth import require_owner
from veya.rkz import routines

router = APIRouter(prefix="/api/v1/rkz/routines", tags=["rkz-routines"])


def _check_feature_flag() -> None:
    # Flag VEYA_RKZ_ROUTINES default OFF. If OFF or not "1"/"true", owner gets 404.
    flag = os.environ.get("VEYA_RKZ_ROUTINES", "").lower()
    if flag not in {"1", "true", "yes", "on"}:
        raise HTTPException(status_code=404, detail="Routines feature flag disabled")


@router.get("", dependencies=[Depends(require_owner)])
async def api_list_routines() -> dict[str, Any]:
    _check_feature_flag()
    return {"status": "ok", "routines": routines.list_routines()}


@router.get("/{name}", dependencies=[Depends(require_owner)])
async def api_get_routine(name: str) -> dict[str, Any]:
    _check_feature_flag()
    try:
        data = routines.get_routine(name)
        return {"status": "ok", "routine": data["meta"], "body": data["body"], "content": data["content"]}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("", dependencies=[Depends(require_owner)])
async def api_create_routine(request: Request) -> dict[str, Any]:
    _check_feature_flag()
    body_bytes = await request.body()
    content = body_bytes.decode("utf-8", errors="ignore")
    try:
        meta = routines.save_routine(content)
        return {"status": "ok", "routine": meta}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{name}", dependencies=[Depends(require_owner)])
async def api_update_routine(name: str, request: Request) -> dict[str, Any]:
    _check_feature_flag()
    body_bytes = await request.body()
    content = body_bytes.decode("utf-8", errors="ignore")
    try:
        parsed = routines.validate_routine_content(content)
        if str(parsed["meta"]["name"]) != name:
            raise ValueError("Routine name in front matter does not match URL name")
        meta = routines.save_routine(content)
        return {"status": "ok", "routine": meta}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{name}", dependencies=[Depends(require_owner)])
async def api_delete_routine(name: str, request: Request) -> dict[str, Any]:
    _check_feature_flag()
    # Check confirm query param or json body confirm=true
    confirm = request.query_params.get("confirm", "").lower() in {"1", "true", "yes"}
    if not confirm:
        try:
            data = await request.json()
            if isinstance(data, dict) and data.get("confirm") is True:
                confirm = True
        except Exception:
            pass
    
    if not confirm:
        raise HTTPException(status_code=400, detail="Delete requires confirmation (confirm=true)")
    
    try:
        routines.delete_routine(name, confirm=True)
        return {"status": "ok", "message": f"Routine '{name}' deleted"}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
