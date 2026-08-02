"""Model catalog route: list available models per provider."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/models", tags=["models"])

_CATALOG = {
    "dashscope": ["qwen-plus", "qwen-max", "qwen-turbo"],
    "anthropic": ["claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-opus-4-8"],
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo"],
}


@router.get("")
async def list_models() -> dict:
    return {"providers": _CATALOG}


@router.get("/{provider}")
async def list_provider_models(provider: str) -> dict:
    if provider not in _CATALOG:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider!r}")
    return {"provider": provider, "models": _CATALOG[provider]}
