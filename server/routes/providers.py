"""Veya Provider Registry endpoints (NANO-W8 VEYA_PROVIDER_REGISTRY).

Unified provider registry for model sources:
- Builtin providers with capabilities
- Custom provider registration (OpenAI-compatible base_url)
- Per-user encrypted API key storage
- CLI agent detection
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.feature_flags import enabled as flag_enabled

router = APIRouter(prefix="/api/v1/providers", tags=["providers"])


def _require_provider_registry() -> None:
    if not flag_enabled("VEYA_PROVIDER_REGISTRY"):
        raise HTTPException(status_code=404, detail="provider registry not enabled (VEYA_PROVIDER_REGISTRY=1)")


def get_provider_registry():
    """Get the VeyaProviderRegistry instance."""
    from veya.obase.provider_registry import get_veya_provider_registry
    return get_veya_provider_registry()


# ── Request Models ────────────────────────────────────────────────────────

class ProviderSpecRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=120)
    protocol_family: str = Field(..., pattern="^(openai|anthropic|gemini|custom)$")
    base_url: str | None = Field(default=None, max_length=512)
    capabilities: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    default_model: str | None = Field(default=None, max_length=128)
    description: str = Field(default="", max_length=500)


class ApiKeyRequest(BaseModel):
    provider: str = Field(..., min_length=1, max_length=64)
    api_key: str = Field(..., min_length=1)


class BotModelSourceRequest(BaseModel):
    bot_id: str = Field(..., min_length=1, max_length=64)
    source: str = Field(..., pattern="^(server|user_key|cli)$")
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    api_key_ref: str | None = Field(default=None, max_length=128)
    cli_agent: str | None = Field(default=None, max_length=64)
    cli_args: list[str] = Field(default_factory=list)


# ── Endpoints ─────────────────────────────────────────────────────────────

def _require_bot_owner(bot_id: str, user: dict[str, Any]) -> None:
    """Refuse a model-source change for a bot the caller does not own.

    Missing non-default bots are rejected because there is no owner record to
    authorize against. System default bots are the only explicit exception.
    """
    from server.goal_run.bot_identity import BotRegistry

    spec = BotRegistry().get(bot_id)
    from server.goal_run.bot_identity import DEFAULT_BOT_ID
    from server.routes.bot_team import CHEF_BOT_ID
    if spec is None:
        if bot_id in {DEFAULT_BOT_ID, CHEF_BOT_ID}:
            return
        raise HTTPException(status_code=404, detail="bot not found")

    owner = str(spec.owner_id or "").strip()
    if bot_id not in {DEFAULT_BOT_ID, CHEF_BOT_ID} and (
        not owner or owner in {"system", "default", "anonymous"}
        or owner != str(user.get("user_id") or "")
    ):
        raise HTTPException(status_code=404, detail="bot not found")


@router.get("")
async def list_providers(
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """List all providers (builtin + custom) with capabilities."""
    _require_provider_registry()
    registry = get_provider_registry()
    providers = registry.get_all_providers()
    cli_agents = registry.get_cli_agents()
    user_keys = registry.list_user_providers(user["user_id"])

    return {
        "providers": {
            name: {
                "name": spec.name,
                "display_name": spec.display_name,
                "protocol_family": spec.protocol_family,
                "base_url": spec.base_url,
                "capabilities": spec.ordered_capabilities,
                "models": spec.models,
                "default_model": spec.default_model,
                "description": spec.description,
                "available": spec.available,
                "requires_api_key": spec.requires_api_key,
                "has_user_key": name in user_keys,
            }
            for name, spec in providers.items()
        },
        "cli_agents": cli_agents,
        "user_keys": user_keys,
    }


@router.post("/custom")
async def register_custom_provider(
    req: ProviderSpecRequest,
    user: dict[str, Any] = Depends(auth_mod.require_owner),
) -> dict[str, Any]:
    """Register a custom OpenAI-compatible provider."""
    _require_provider_registry()
    registry = get_provider_registry()

    from veya.obase.provider_registry import VeyaProviderSpec
    spec = VeyaProviderSpec(
        name=req.name,
        display_name=req.display_name,
        protocol_family=req.protocol_family,
        base_url=req.base_url,
        capabilities=set(req.capabilities),
        models=req.models,
        default_model=req.default_model,
        description=req.description,
    )
    try:
        registry.register_custom_provider(spec)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "created", "provider": spec.name}


@router.delete("/custom/{name}")
async def unregister_custom_provider(
    name: str,
    user: dict[str, Any] = Depends(auth_mod.require_owner),
) -> dict[str, Any]:
    """Unregister a custom provider."""
    _require_provider_registry()
    registry = get_provider_registry()
    if not registry.unregister_custom_provider(name):
        raise HTTPException(status_code=404, detail=f"custom provider {name!r} not found")
    return {"status": "deleted", "provider": name}


@router.post("/keys")
async def set_user_api_key(
    req: ApiKeyRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Store encrypted API key for the current user."""
    _require_provider_registry()
    registry = get_provider_registry()
    registry.set_user_api_key(user["user_id"], req.provider, req.api_key)
    return {"status": "stored", "provider": req.provider}


@router.delete("/keys/{provider}")
async def delete_user_api_key(
    provider: str,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Delete a user's API key for a provider."""
    _require_provider_registry()
    registry = get_provider_registry()
    if not registry.delete_user_api_key(user["user_id"], provider):
        raise HTTPException(status_code=404, detail=f"API key for {provider!r} not found")
    return {"status": "deleted", "provider": provider}


@router.get("/cli-agents")
async def list_cli_agents(
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """List detected CLI agents."""
    _require_provider_registry()
    registry = get_provider_registry()
    return {"cli_agents": registry.get_cli_agents()}


@router.post("/bot-model-source")
async def set_bot_model_source(
    req: BotModelSourceRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Set the model source for a bot."""
    _require_provider_registry()
    _require_bot_owner(req.bot_id, user)
    registry = get_provider_registry()

    from veya.obase.provider_registry import ModelSourceConfig
    config = ModelSourceConfig(
        source=req.source,
        provider=req.provider or "",
        model=req.model or "",
        api_key_ref=req.api_key_ref,
        cli_agent=req.cli_agent,
        cli_args=req.cli_args,
    )
    registry.set_bot_model_source(req.bot_id, config)
    return {"status": "updated", "bot_id": req.bot_id, "source": req.source}


@router.get("/bot-model-source/{bot_id}")
async def get_bot_model_source(
    bot_id: str,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Get the model source configuration for a bot."""
    _require_provider_registry()
    _require_bot_owner(bot_id, user)
    registry = get_provider_registry()
    config = registry.get_bot_model_source(bot_id)
    if config is None:
        return {"bot_id": bot_id, "source": "server", "provider": "", "model": ""}
    return {"bot_id": bot_id, **config.to_dict()}


@router.get("/effective-model/{bot_id}")
async def get_effective_model(
    bot_id: str,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Get the resolved effective model configuration for a bot."""
    _require_provider_registry()
    _require_bot_owner(bot_id, user)
    registry = get_provider_registry()
    config = registry.get_effective_model_config(bot_id, user["user_id"])
    return {"bot_id": bot_id, **config}


__all__ = ["router"]
