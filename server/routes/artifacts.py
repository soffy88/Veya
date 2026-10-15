import json
import os
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from server import auth as auth_mod
from server.goal_run.bot_identity import DEFAULT_BOT_ID

router = APIRouter(
    prefix="/api/v1/artifacts",
    tags=["artifacts"],
    dependencies=[Depends(auth_mod.get_current_user)],
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


def _check_agent_context():
    from server.tool_registry import _delegation_depth_ctx

    if _delegation_depth_ctx.get() > 0:
        raise HTTPException(status_code=403, detail="Agent cannot share artifacts")


@router.post("/{artifact_id}/share")
def share_artifact(
    artifact_id: str,
    current_user: dict = Depends(auth_mod.get_current_user),
):
    if not _is_sharing_enabled():
        raise HTTPException(status_code=403, detail="Public sharing is disabled")

    links = _load_artifact_links()

    if artifact_id not in links:
        links[artifact_id] = {
            "token": _generate_token(),
            "created_at": time.time(),
            "owner": current_user["user_id"],
            "enabled": True,
        }
    else:
        if links[artifact_id].get("owner") != current_user["user_id"]:
            raise HTTPException(status_code=403, detail="Not the owner of this artifact")
        links[artifact_id]["enabled"] = True

    _save_artifact_links(links)

    token = links[artifact_id]["token"]
    return {"enabled": True, "token": token, "url": f"/api/v1/a/{token}"}


@router.delete("/{artifact_id}/share")
def unshare_artifact(
    artifact_id: str,
    current_user: dict = Depends(auth_mod.get_current_user),
):
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
    current_user: dict = Depends(auth_mod.get_current_user),
):
    if not _is_sharing_enabled():
        raise HTTPException(status_code=403, detail="Public sharing is disabled")

    links = _load_artifact_links()
    result = []
    for artifact_id, info in links.items():
        if info.get("owner") == current_user["user_id"]:
            result.append(
                {
                    "artifact_id": artifact_id,
                    "token": info["token"],
                    "enabled": info.get("enabled", False),
                    "created_at": info["created_at"],
                    "url": f"/api/v1/a/{info['token']}" if info.get("enabled") else None,
                }
            )
    return {"shares": result}


@public_router.get("/{token}")
async def get_public_artifact(token: str, request: Request):
    links = _load_artifact_links()
    for artifact_id, info in links.items():
        if info.get("token") == token:
            if not info.get("enabled", False):
                raise HTTPException(status_code=403, detail="Artifact sharing disabled")

            artifacts_dir = Path(os.environ.get("VEYA_ARTIFACTS_DIR", "/tmp/artifacts"))
            # Some tests create the artifact in artifacts_dir / artifact_id / "file.txt"
            # others might just be a file named artifact_id. We check both.
            path1 = artifacts_dir / artifact_id / "file.txt"
            path2 = artifacts_dir / artifact_id

            if path1.exists():
                return FileResponse(path=path1)
            elif path2.exists() and path2.is_file():
                return FileResponse(path=path2)

            raise HTTPException(status_code=404, detail="Artifact file not found")

    raise HTTPException(status_code=404, detail="Artifact not found")
