"""server/routes/cli_agents.py — CLI coding agent registry API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from server import auth as auth_mod
from pydantic import BaseModel

from runtime.coding.cli_agents import (
    CLIAgentRegistry,
    get_cli_agent_registry,
    _validate_command_template,
)

router = APIRouter(tags=["cli-agents"])


class CustomProfileRequest(BaseModel):
    name: str
    command_template: str


class CustomProfileResponse(BaseModel):
    name: str
    command_template: str
    is_custom: bool
    available: bool


def _get_repo_root() -> Path:
    import os

    root = os.environ.get("VEYA_PROJECT_ROOT", ".")
    return Path(root).expanduser().resolve()


@router.get("/api/v1/agents/cli")
async def list_cli_agents(user: dict[str, Any] = Depends(auth_mod.require_user)) -> dict[str, list[dict[str, Any]]]:
    """List all CLI agent profiles (built-in + custom)."""
    repo_root = _get_repo_root()
    registry = get_cli_agent_registry(repo_root)
    profiles = registry.list_all()
    return {"profiles": [p.to_dict() for p in profiles]}


@router.post("/api/v1/agents/cli/custom", response_model=CustomProfileResponse)
async def register_custom_cli_agent(
    req: CustomProfileRequest,
    user: dict[str, Any] = Depends(auth_mod.require_owner),
) -> dict[str, Any]:
    """Register a custom CLI agent profile."""
    repo_root = _get_repo_root()
    registry = get_cli_agent_registry(repo_root)

    try:
        _validate_command_template(req.command_template)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        profile = registry.register_custom(req.name, req.command_template)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return profile.to_dict()


__all__ = ["router"]
