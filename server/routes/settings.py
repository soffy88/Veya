"""Settings routes — owner-only configuration toggles.

ORCA-W4: Artifact sharing global toggle.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import auth as auth_mod
from server.goal_run.bot_identity import DEFAULT_BOT_ID
from server.tool_registry import _delegation_depth_ctx

router = APIRouter(
    prefix="/api/v1/settings",
    tags=["settings"],
    dependencies=[Depends(auth_mod.get_current_user)],
)


class ArtifactSharingSettings(BaseModel):
    """Artifact sharing global settings."""
    enabled: bool


def _get_settings_file(bot_id: str):
    """Get the path to the settings storage file."""
    import os
    from pathlib import Path
    if "VEYA_SETTINGS_PATH" in os.environ:
        return Path(os.environ["VEYA_SETTINGS_PATH"])
    settings_dir = Path(os.environ.get("VEYA_SETTINGS_DIR", os.path.expanduser("~/.veya/settings")))
    return settings_dir / f"{bot_id}.json"


def _load_settings(bot_id: str) -> dict:
    """Load settings from storage."""
    path = _get_settings_file(bot_id)
    if path.exists():
        try:
            import json
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def _save_settings(bot_id: str, settings: dict) -> None:
    """Save settings to storage."""
    path = _get_settings_file(bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    path.write_text(json.dumps(settings, indent=2))


def _check_agent_context() -> None:
    """Reject if called from an agent/tool context.

    Per spec: the artifact sharing toggle must only be changeable via
    authenticated REST, NOT by any model tool. We reject if the current
    delegation depth > 0 (meaning we're inside an agent/tool call).
    """
    depth = _delegation_depth_ctx.get()
    if depth > 0:
        raise HTTPException(
            status_code=403,
            detail="Artifact sharing settings can only be changed via direct REST call, not from agent/tool context"
        )


@router.get("/artifacts")
def get_artifact_sharing_settings(
    bot_id: str = DEFAULT_BOT_ID,
    current_user: str = Depends(auth_mod.get_current_user),
):
    """Get artifact sharing settings (owner only)."""
    settings = _load_settings(bot_id)
    artifact_settings = settings.get("artifact_sharing", {"enabled": False})
    return ArtifactSharingSettings(**artifact_settings)


@router.put("/artifacts")
def update_artifact_sharing_settings(
    req: ArtifactSharingSettings,
    bot_id: str = DEFAULT_BOT_ID,
    current_user: str = Depends(auth_mod.get_current_user),
):
    """Update artifact sharing settings (owner only).

    Rejects if called from an agent/tool context (delegation_depth > 0).
    """
    _check_agent_context()
    settings = _load_settings(bot_id)
    settings["artifact_sharing"] = {"enabled": req.enabled}
    _save_settings(bot_id, settings)
    return ArtifactSharingSettings(**settings["artifact_sharing"])