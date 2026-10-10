"""server/routes/usage.py — Quota meter API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from runtime.coding.quota import QuotaMeter, get_quota_meter
from runtime.coding.cli_agents import get_cli_agent_registry

router = APIRouter(tags=["usage"])


class QuotaInfoResponse(BaseModel):
    backend: str
    account: str = "unknown"
    used: str | int = "unknown"
    limit: str | int = "unknown"
    reset_at: str = "unknown"
    unit: str = "unknown"


class QuotaResponse(BaseModel):
    quotas: list[QuotaInfoResponse]


def _get_repo_root() -> Path:
    import os

    root = os.environ.get("VEYA_PROJECT_ROOT", ".")
    return Path(root).expanduser().resolve()


@router.get("/api/v1/usage/quota", response_model=QuotaResponse)
async def get_quota() -> dict[str, list[dict[str, Any]]]:
    """Get quota information for all detected CLI agents."""
    repo_root = _get_repo_root()

    # Get all known backend names from CLI agent registry
    cli_registry = get_cli_agent_registry(repo_root)
    cli_profiles = cli_registry.list_all()
    backend_names = [p.name for p in cli_profiles if p.available]

    # Also include any registered backends from the backend registry
    try:
        from server.backends import get_backend_registry

        backend_registry = get_backend_registry()
        for b in backend_registry.list():
            if b["name"] not in backend_names:
                backend_names.append(b["name"])
    except Exception:
        pass

    # Get quota for each backend
    meter = get_quota_meter(repo_root)
    quotas = meter.get_all_quotas(backend_names)

    return {"quotas": [q.to_dict() for q in quotas]}


__all__ = ["router"]
