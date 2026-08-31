"""Layer-4 product shell for the default Veya Bot.

This module is deliberately a thin adapter around the existing product config
written by ``veya init``.  It describes the product instance and its bindings;
execution state remains owned by MasterAgent, GoalRun, and the existing
governance/computer/memory services.

Only credential references are exposed or persisted here.  Raw credentials
remain in the existing CLI/environment or browser-local configuration paths.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from cli.product import _load_config, _save_config
from veya.obase._llm_config import _API_KEY_ENV, _DEFAULT_MODELS

DEFAULT_BOT_ID = "veya-default"
DEFAULT_BOT_NAME = "Veya Bot"
PRODUCT_CONFIG_VERSION = 1

Lifecycle = Literal["uninitialized", "ready", "degraded"]

_BINDINGS: dict[str, Any] = {
    "memory": {
        "semantic": "MemoryController / Personal Runtime",
        "preference": "memory_bank",
    },
    "skills": {"source": "Personal Runtime / SkillRegistry"},
    "computer": {"lifecycle": "ComputerSupervisorEngine", "profile": "local-worktree"},
    "browser": {
        "lifecycle": "BrowserComputer",
        "control_states": ["AGENT_CONTROL", "HUMAN_CONTROL"],
    },
    "tools": {
        "action_gateway": "ActionGatewayEngine",
        "mcp": "MCPRegistryEngine",
    },
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _provider_info(config: dict[str, Any]) -> dict[str, Any]:
    llm = _mapping(config.get("llm"))
    provider = str(llm.get("provider") or "dashscope")
    provider_config = _mapping(_mapping(config.get("providers")).get(provider))
    env_name = _API_KEY_ENV.get(provider, "")
    # A local provider needs no credential.  For remote providers, the actual
    # secret is intentionally inspected only to compute a boolean and is never
    # copied into the response or the product metadata.
    configured = provider == "ollama" or bool(
        provider_config.get("api_key")
        or provider_config.get("credential_ref")
        or (env_name and os.environ.get(env_name))
    )
    model = str(llm.get("model") or _DEFAULT_MODELS.get(provider) or "")
    credential_ref = provider_config.get("credential_ref")
    return {
        "id": provider,
        "model": model,
        "configured": configured,
        "credential": {
            "configured": configured,
            "ref": str(credential_ref) if credential_ref else None,
        },
    }


def _workspace_info(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("workspace")
    if not raw:
        return {"path": None, "configured": False, "exists": False}
    path = Path(str(raw)).expanduser()
    return {
        "path": str(path),
        "configured": True,
        "exists": path.is_dir(),
    }


def read_bot_state(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a secret-free, rebuildable snapshot of the default Bot setup."""

    cfg = config if config is not None else _load_config()
    bot = _mapping(cfg.get("bot"))
    provider = _provider_info(cfg)
    workspace = _workspace_info(cfg)
    # ``veya init`` predates this product-shell metadata.  A valid existing
    # init config is therefore treated as completed onboarding for backwards
    # compatibility; no migration or second config store is introduced.
    legacy_setup = bool(cfg.get("workspace") and workspace["exists"] and provider["configured"])
    onboarding_completed = bool(bot.get("onboarding_completed", False)) or legacy_setup
    ready = bool(
        onboarding_completed
        and provider["configured"]
        and provider["model"]
        and workspace["exists"]
    )
    lifecycle: Lifecycle = (
        "ready" if ready else ("degraded" if onboarding_completed else "uninitialized")
    )

    return {
        "bot": {
            "id": str(bot.get("id") or DEFAULT_BOT_ID),
            "name": str(bot.get("name") or DEFAULT_BOT_NAME),
            "lifecycle": lifecycle,
        },
        "onboarding": {
            "version": PRODUCT_CONFIG_VERSION,
            "completed": onboarding_completed,
            "required": not onboarding_completed,
        },
        "provider": provider,
        "workspace": workspace,
        "bindings": _BINDINGS,
        "runtime": {"status": "running", "authority": "MasterAgent"},
        "recovery": {
            "sessions": "/api/v1/sessions",
            "tasks": "/api/v1/tasks",
            "workbench": "/api/v1/workbench/{task_id}",
        },
    }


def configure_bot(
    *,
    provider: str | None = None,
    model: str | None = None,
    workspace: str | None = None,
    credential_ref: str | None = None,
) -> dict[str, Any]:
    """Persist onboarding metadata through the existing product config path.

    ``credential_ref`` is a stable reference only; this function deliberately
    has no ``api_key`` argument so the product API cannot become a second
    credential sink.
    """

    config = _load_config()
    llm = _mapping(config.get("llm"))
    selected_provider = str(provider or llm.get("provider") or "dashscope").strip()
    if not selected_provider:
        raise ValueError("provider must not be empty")
    selected_model = str(model or llm.get("model") or "").strip()
    llm["provider"] = selected_provider
    if selected_model:
        llm["model"] = selected_model
    config["llm"] = llm

    if workspace is not None and workspace.strip():
        path = Path(workspace).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"workspace does not exist: {path}")
        config["workspace"] = str(path)

    providers = _mapping(config.get("providers"))
    provider_config = _mapping(providers.get(selected_provider))
    if credential_ref is not None and credential_ref.strip():
        provider_config["credential_ref"] = credential_ref.strip()
    providers[selected_provider] = provider_config
    config["providers"] = providers

    current_bot = _mapping(config.get("bot"))
    current_bot.update(
        {
            "id": str(current_bot.get("id") or DEFAULT_BOT_ID),
            "name": str(current_bot.get("name") or DEFAULT_BOT_NAME),
            "config_version": PRODUCT_CONFIG_VERSION,
            "onboarding_completed": True,
            "bindings": _BINDINGS,
        }
    )
    config["bot"] = current_bot
    _save_config(config)
    return read_bot_state(config)


__all__ = ["DEFAULT_BOT_ID", "DEFAULT_BOT_NAME", "configure_bot", "read_bot_state"]
