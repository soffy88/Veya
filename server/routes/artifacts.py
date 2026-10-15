import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from server import auth as auth_mod
from server.goal_run.bot_identity import DEFAULT_BOT_ID

router = APIRouter(
    prefix="/api/v1/artifacts",
    tags=["artifacts"],
    dependencies=[Depends(auth_mod.require_user)],
)

public_router = APIRouter(prefix="/api/v1/a", tags=["public-artifacts"])

def _get_artifact_links_file() -> Path:
    artifacts_dir = Path(os.environ.get("VEYA_ARTIFACTS_DIR", "/tmp/artifacts"))
    return artifacts_dir / "artifact_links.json"

def _load_artifact_links() -> dict:
    path = _get_artifact_links_file()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}

def _save_artifact_links(links: dict) -> None:
    path = _get_artifact_links_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(links, indent=2))

def _generate_token() -> str:
    return secrets.token_urlsafe(24)

def _is_sharing_enabled() -> bool:
    from server.routes.settings import _load_settings
    settings = _load_settings(DEFAULT_BOT_ID)
    return settings.get("artifact_sharing", {}).get("enabled", False)


def _safe_artifact_id(artifact_id: str) -> bool:
    return bool(
        isinstance(artifact_id, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", artifact_id)
        and artifact_id not in {".", "..", "artifact_links.json"}
    )


def _artifact_file(artifact_id: str) -> Path | None:
    """Resolve the published file only when it remains inside the artifact root."""
    if not _safe_artifact_id(artifact_id):
        return None
    root = Path(os.environ.get("VEYA_ARTIFACTS_DIR", "/tmp/artifacts")).expanduser().resolve()
    candidates = (root / artifact_id / "file.txt", root / artifact_id)
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            if resolved.is_relative_to(root) and resolved.is_file():
                return resolved
        except (OSError, RuntimeError):
            continue
    return None


def _artifact_is_owned_by(artifact_id: str, user_id: str) -> bool:
    """Require publisher metadata for a first share; never let a caller claim an ID."""
    file_path = _artifact_file(artifact_id)
    if file_path is None:
        return False
    root = Path(os.environ.get("VEYA_ARTIFACTS_DIR", "/tmp/artifacts")).expanduser().resolve()
    artifact_dir = file_path.parent
    if artifact_dir == root:
        # A standalone file requires its sibling manifest.
        manifest = root / f"{artifact_id}.artifact.json"
    else:
        manifest = artifact_dir / "artifact.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    metadata = data.get("metadata") if isinstance(data, dict) else None
    owners = {
        str(data.get("owner_user_id") or "") if isinstance(data, dict) else "",
        str(data.get("owner") or "") if isinstance(data, dict) else "",
        str(metadata.get("owner_user_id") or "") if isinstance(metadata, dict) else "",
    }
    return bool(user_id and user_id in owners)

def _check_agent_context():
    from server.tool_registry import _delegation_depth_ctx
    if _delegation_depth_ctx.get() > 0:
        raise HTTPException(status_code=403, detail="Agent cannot share artifacts")

@router.post("/{artifact_id}/share")
def share_artifact(
    artifact_id: str,
    current_user: dict = Depends(auth_mod.require_user),
):
    if not _safe_artifact_id(artifact_id):
        raise HTTPException(status_code=400, detail="Invalid artifact ID")
    if not _is_sharing_enabled():
        raise HTTPException(status_code=403, detail="Public sharing is disabled")

    links = _load_artifact_links()

    if artifact_id not in links:
        if not _artifact_is_owned_by(artifact_id, str(current_user.get("user_id") or "")):
            raise HTTPException(status_code=404, detail="Owned published artifact not found")
        links[artifact_id] = {
            "token": _generate_token(),
            "created_at": time.time(),
            "owner": current_user["user_id"],
            "enabled": True
        }
    else:
        if links[artifact_id].get("owner") != current_user["user_id"]:
            raise HTTPException(status_code=403, detail="Not the owner of this artifact")
        links[artifact_id]["enabled"] = True

    _save_artifact_links(links)

    token = links[artifact_id]["token"]
    return {
        "enabled": True,
        "token": token,
        "url": f"/api/v1/a/{token}"
    }

@router.delete("/{artifact_id}/share")
def unshare_artifact(
    artifact_id: str,
    current_user: dict = Depends(auth_mod.require_user),
):
    if not _safe_artifact_id(artifact_id):
        raise HTTPException(status_code=400, detail="Invalid artifact ID")
    if not _is_sharing_enabled():
        raise HTTPException(status_code=403, detail="Public sharing is disabled")

    links = _load_artifact_links()
    if artifact_id not in links:
        raise HTTPException(status_code=404, detail="Artifact not shared")

    if links[artifact_id].get("owner") != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Not the owner of this artifact")

    del links[artifact_id]
    _save_artifact_links(links)
    return {"deleted": True}

@router.get("/shares")
def list_shared_artifacts(
    current_user: dict = Depends(auth_mod.require_user),
):
    if not _is_sharing_enabled():
        raise HTTPException(status_code=403, detail="Public sharing is disabled")

    links = _load_artifact_links()
    result = []
    for artifact_id, info in links.items():
        if info.get("owner") == current_user["user_id"]:
            result.append({
                "artifact_id": artifact_id,
                "enabled": info.get("enabled", False),
                "created_at": info.get("created_at"),
                "url": f"/api/v1/a/{info['token']}" if info.get("enabled") else None,
            })
    return {"shares": result}

@public_router.get("/{token}")
async def get_public_artifact(token: str, request: Request):
    links = _load_artifact_links()
    for artifact_id, info in links.items():
        if info.get("token") == token:
            if not info.get("enabled", False):
                raise HTTPException(status_code=403, detail="Artifact sharing disabled")
            
            artifact_path = _artifact_file(artifact_id)
            if artifact_path is not None:
                return FileResponse(path=artifact_path)
            raise HTTPException(status_code=404, detail="Artifact file not found")
            
    raise HTTPException(status_code=404, detail="Artifact not found")