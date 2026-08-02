"""Auth route: API key setup and provider login."""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/auth", tags=["auth"])


class SetKeyRequest(BaseModel):
    provider: str
    api_key: str


_KEY_ENV_NAMES = {
    "dashscope": "DASHSCOPE_API_KEY",
    "anthropic":  "ANTHROPIC_API_KEY",
    "openai":     "OPENAI_API_KEY",
}


@router.post("/key")
async def set_api_key(req: SetKeyRequest) -> dict:
    env_name = _KEY_ENV_NAMES.get(req.provider.lower())
    if not env_name:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {req.provider!r}")
    os.environ[env_name] = req.api_key
    return {"status": "set", "provider": req.provider}


@router.get("/providers")
async def list_providers() -> dict:
    return {
        "providers": [
            {
                "name": p,
                "env_var": env,
                "configured": bool(os.environ.get(env)),
            }
            for p, env in _KEY_ENV_NAMES.items()
        ]
    }
