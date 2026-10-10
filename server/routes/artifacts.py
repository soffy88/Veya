"""Artifact sharing routes — token-based public links for artifacts.

ORCA-W4: Public artifact links with owner-only enable/disable toggle.
"""
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from server import auth as auth_mod
from server.goal_run.project_store import ProjectStore
from server.goal_run.bot_identity import DEFAULT_BOT_ID, get_current_bot_id

router = APIRouter(
    prefix="/api/v1/artifacts",
    tags=["artifacts"],
    dependencies=[Depends(auth_mod.get_current_user)],
)


class ArtifactShareRequest(BaseModel):
    """Request to enable/disable public sharing for an artifact."""
    artifact_path: str
    enabled: bool


def _get_artifact_links_file(bot_id: str) -> Path:
    """Get the path to the artifact links storage file."""
    store = ProjectStore(bot_id=bot_id)
    return store.root / "artifact_links.json"


def _load_artifact_links(bot_id: str) -> dict:
    """Load artifact links from storage."""
    path = _get_artifact_links_file(bot_id)
    if path.exists():
        try:
            import json
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def _save_artifact_links(bot_id: str, links: dict) -> None:
    """Save artifact links to storage."""
    path = _get_artifact_links_file(bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    path.write_text(json.dumps(links, indent=2))


def _generate_token() -> str:
    """Generate a secure random token for artifact sharing."""
    return secrets.token_urlsafe(32)


@router.post("/share")
def share_artifact(
    bot_id: str,
    req: ArtifactShareRequest,
    current_user: str = Depends(auth_mod.get_current_user),
):
    """Enable or disable public sharing for an artifact.

    Only the artifact owner (current user) can toggle sharing.
    Returns the public token when enabled.
    """
    # Verify artifact exists
    artifact_path = Path(req.artifact_path)
    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")

    # Load existing links
    links = _load_artifact_links(bot_id)
    artifact_key = str(artifact_path.absolute())

    if req.enabled:
        # Enable sharing: generate token if not exists
        if artifact_key not in links:
            links[artifact_key] = {
                "token": _generate_token(),
                "created_at": time.time(),
                "owner": current_user,
            }
        elif links[artifact_key].get("owner") != current_user:
            raise HTTPException(status_code=403, detail="Not the owner of this artifact")
        links[artifact_key]["enabled"] = True
    else:
        # Disable sharing
        if artifact_key not in links:
            raise HTTPException(status_code=404, detail="Artifact not shared")
        if links[artifact_key].get("owner") != current_user:
            raise HTTPException(status_code=403, detail="Not the owner of this artifact")
        links[artifact_key]["enabled"] = False

    _save_artifact_links(bot_id, links)

    if req.enabled:
        return {
            "enabled": True,
            "token": links[artifact_key]["token"],
            "public_url": f"/api/v1/a/{links[artifact_key]['token']}",
        }
    return {"enabled": False}


@router.get("/list")
def list_shared_artifacts(
    bot_id: str,
    current_user: str = Depends(auth_mod.get_current_user),
):
    """List all shared artifacts for the current user."""
    links = _load_artifact_links(bot_id)
    result = []
    for artifact_key, info in links.items():
        if info.get("owner") == current_user:
            result.append({
                "artifact_path": artifact_key,
                "token": info["token"],
                "enabled": info.get("enabled", False),
                "created_at": info["created_at"],
                "public_url": f"/api/v1/a/{info['token']}" if info.get("enabled") else None,
            })
    return {"artifacts": result}


@router.delete("/share/{artifact_path:path}")
def unshare_artifact(
    bot_id: str,
    artifact_path: str,
    current_user: str = Depends(auth_mod.get_current_user),
):
    """Disable and remove public sharing for an artifact."""
    links = _load_artifact_links(bot_id)
    artifact_key = str(Path(artifact_path).absolute())

    if artifact_key not in links:
        raise HTTPException(status_code=404, detail="Artifact not shared")

    if links[artifact_key].get("owner") != current_user:
        raise HTTPException(status_code=403, detail="Not the owner of this artifact")

    del links[artifact_key]
    _save_artifact_links(bot_id, links)
    return {"deleted": True}


# Public endpoint - no auth required
public_router = APIRouter(prefix="/api/v1/a", tags=["public-artifacts"])


@public_router.get("/{token}")
async def get_public_artifact(token: str, request: Request):
    """Get a public artifact by token.

    Returns 403 if sharing is disabled, 404 if token not found.
    """
    # We need to search across all bots for this token
    # For simplicity, search in default bot first, then check all
    bot_ids = [DEFAULT_BOT_ID]
    # Could add logic to scan for other bot directories

    for bot_id in bot_ids:
        links = _load_artifact_links(bot_id)
        for artifact_key, info in links.items():
            if info.get("token") == token:
                if not info.get("enabled", False):
                    raise HTTPException(status_code=403, detail="Artifact sharing disabled")
                
                artifact_path = Path(artifact_key)
                if not artifact_path.exists():
                    raise HTTPException(status_code=404, detail="Artifact file not found")
                
                # Return the file
                from fastapi.responses import FileResponse
                return FileResponse(
                    path=artifact_path,
                    filename=artifact_path.name,
                )
    
    raise HTTPException(status_code=404, detail="Artifact not found")