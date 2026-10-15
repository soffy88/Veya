"""Owner-only REST surface for opt-in external CLI runtimes."""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from runtime.coding.cli_agents import xrt_feature_enabled, xrt_runtime_enabled
from server.routes.tailnet import require_tailnet_owner
from veya.remote import xrt_runtime


def require_xrt_enabled() -> None:
    if not xrt_feature_enabled():
        raise HTTPException(status_code=404, detail="XRT is disabled")


router = APIRouter(
    prefix="/api/v1/xrt",
    tags=["external-runtimes"],
    dependencies=[Depends(require_xrt_enabled), Depends(require_tailnet_owner)],
)


class XRTDispatchRequest(BaseModel):
    runtime: str = Field(min_length=1, max_length=40)
    task: str = Field(min_length=1, max_length=12000)
    timeout_s: float = Field(default=120.0, ge=1, le=1800)


@router.get("/runtimes")
async def list_xrt_runtimes() -> dict[str, Any]:
    return xrt_runtime.list_runtimes(os.getenv("VEYA_PROJECT_ROOT", "."))


@router.post("/dispatch")
async def dispatch_xrt_task(req: XRTDispatchRequest) -> dict[str, Any]:
    if not xrt_runtime_enabled(req.runtime):
        raise HTTPException(status_code=404, detail="XRT runtime is disabled")
    try:
        return await xrt_runtime.dispatch(
            req.runtime, req.task, os.getenv("VEYA_PROJECT_ROOT", "."), req.timeout_s
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/cancel/{execution_id}")
async def cancel_xrt_task(execution_id: str) -> dict[str, Any]:
    cancelled = await xrt_runtime.cancel(execution_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="Execution is not running")
    return {"execution_id": execution_id, "cancelled": True}


@router.get("/status/{execution_id}")
async def get_xrt_status(execution_id: str) -> dict[str, Any]:
    status = xrt_runtime.get_status(execution_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    return status


__all__ = ["router", "require_xrt_enabled"]
