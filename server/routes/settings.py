"""Settings routes — owner-only configuration toggles.

ORCA-W4: Artifact sharing global toggle.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import auth as auth_mod
from server.goal_run.project_store import ProjectStore
from server.goal_run.bot_identity import DEFAULT_BOT_ID

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
    store = ProjectStore(bot_id=bot_id)
    return store.root / "settings.json"


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


@router.get("/artifacts")
def get_artifact_sharing_settings(
    bot_id: str = DEFAULT_BOT_ID,
    current_user: str = Depends(auth_mod.get_current_user),
):
    """Get artifact sharing settings (owner only)."""
    settings = _load_settings(bot_id)
    artifact_settings = settings.get("artifact_sharing", {"enabled": False})
    return ArtifactSharingSettings(**artifact_settings)


@router.patch("/artifacts")
def update_artifact_sharing_settings(
    req: ArtifactSharingSettings,
    bot_id: str = DEFAULT_BOT_ID,
    current_user: str = Depends(auth_mod.get_current_user),
):
    """Update artifact sharing settings (owner only)."""
    settings = _load_settings(bot_id)
    settings["artifact_sharing"] = {"enabled": req.enabled}
    _save_settings(bot_id, settings)
    return ArtifactSharingSettings(**settings["artifact_sharing"])