"""server/routes/workspace.py — Worktree shared paths API."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from runtime.coding.worktree_manifest import (
    WorktreeManifest,
    _validate_relative_path,
    load_manifest,
    write_manifest,
)

router = APIRouter(tags=["workspace"])


class SharedPathsRequest(BaseModel):
    copy_paths: list[str] = Field(default_factory=list, alias="copy")
    share_paths: list[str] = Field(default_factory=list, alias="share")


class SharedPathsResponse(BaseModel):
    copy_paths: list[str] = Field(alias="copy")
    share_paths: list[str] = Field(alias="share")


def _get_repo_root() -> Path:
    """Get the repository root from VEYA_PROJECT_ROOT or cwd."""
    import os

    root = os.environ.get("VEYA_PROJECT_ROOT", ".")
    return Path(root).expanduser().resolve()


@router.get("/api/v1/workspace/shared-paths", response_model=SharedPathsResponse)
async def get_shared_paths() -> dict[str, list[str]]:
    """Get the current worktree shared paths manifest."""
    repo_root = _get_repo_root()
    manifest = load_manifest(repo_root)
    return {"copy": list(manifest.copy), "share": list(manifest.share)}


@router.put("/api/v1/workspace/shared-paths", response_model=SharedPathsResponse)
async def put_shared_paths(req: SharedPathsRequest) -> dict[str, list[str]]:
    """Set the worktree shared paths manifest."""
    repo_root = _get_repo_root()

    # Validate all paths
    try:
        validated_copy = tuple(_validate_relative_path(p) for p in req.copy_paths)
        validated_share = tuple(_validate_relative_path(p) for p in req.share_paths)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    manifest = WorktreeManifest(copy=validated_copy, share=validated_share)
    write_manifest(repo_root, manifest)
    return {"copy": list(manifest.copy), "share": list(manifest.share)}


__all__ = ["router"]
