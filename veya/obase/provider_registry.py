"""
Veya Provider Registry — unified provider registry for model sources (VEYA_PROVIDER_REGISTRY).

Extends the platform 3O obase.provider_registry with Veya-specific features:
- Per-user encrypted API key storage
- Per-bot model source selection (server / user_key / cli)
- Capability flags (vision, image-gen) for UI feature gating
- CLI agent delegation (codex/claude/kimi/gemini)

Single source: re-exports platform 3O ProviderRegistry, adds Veya-specific API.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Import from platform 3O (single source)
try:
    from obase.provider_registry import (
        ProviderRegistry as _PlatformProviderRegistry,
        LLMCaller,
        VLMCaller,
        ImageGenCaller,
        ProviderSpec,
        OBaseRegistryConflict,
        ProviderNotFoundError,
        ProviderDiscoveryError,
        ProtocolFamily,
    )
except ImportError:
    # Fallback if platform 3O not available
    _PlatformProviderRegistry = None
    LLMCaller = None
    VLMCaller = None
    ImageGenCaller = None
    ProviderSpec = None
    OBaseRegistryConflict = Exception
    ProviderNotFoundError = Exception
    ProviderDiscoveryError = Exception
    ProtocolFamily = None


class ProviderRegistryError(Exception):
    """Error in Veya provider registry operations."""


# ── Encryption for API Keys ─────────────────────────────────────────────

def _get_encryption_key() -> bytes:
    """Derive encryption key from VEYA_SECRET_KEY or generate ephemeral."""
    secret = os.environ.get("VEYA_SECRET_KEY", "").encode()
    if not secret:
        # Ephemeral key for dev - NOT for production
        secret = b"veya-dev-key-not-for-production-use-only"
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"veya-provider-registry-salt",
        iterations=100_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(secret))


def _encrypt_value(value: str) -> str:
    """Encrypt a string value."""
    f = Fernet(_get_encryption_key())
    return f.encrypt(value.encode()).decode()


def _decrypt_value(encrypted: str) -> str:
    """Decrypt a string value."""
    f = Fernet(_get_encryption_key())
    return f.decrypt(encrypted.encode()).decode()


# ── Provider Spec Extensions ────────────────────────────────────────────

@dataclass(frozen=True)
class VeyaProviderSpec:
    """Extended provider spec with Veya-specific fields."""
    # Core identity
    name: str
    display_name: str
    protocol_family: str  # "openai", "anthropic", "gemini", "deepseek", "openrouter", "bailian", "kimi", "zhipu", "custom"
    base_url: str | None = None

    # Capabilities
    capabilities: set[str] = field(default_factory=set)  # "vision", "image-gen", "function-calling", "streaming"

    # Model listings
    models: list[str] = field(default_factory=list)
    default_model: str | None = None

    # Availability
    available: bool = True
    requires_api_key: bool = True

    # Metadata
    description: str = ""
    icon: str | None = None


# Predefined provider specs (can be overridden by user config)
BUILTIN_PROVIDERS: dict[str, VeyaProviderSpec] = {
    "openai": VeyaProviderSpec(
        name="openai",
        display_name="OpenAI",
        protocol_family="openai",
        base_url="https://api.openai.com/v1",
        capabilities={"vision", "function-calling", "streaming"},
        models=["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
        default_model="gpt-4o",
        description="OpenAI API",
    ),
    "anthropic": VeyaProviderSpec(
        name="anthropic",
        display_name="Anthropic",
        protocol_family="anthropic",
        base_url="https://api.anthropic.com",
        capabilities={"vision", "function-calling", "streaming"},
        models=["claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"],
        default_model="claude-3-5-sonnet-20241022",
        description="Anthropic API",
    ),
    "gemini": VeyaProviderSpec(
        name="gemini",
        display_name="Google Gemini",
        protocol_family="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        capabilities={"vision", "function-calling", "streaming"},
        models=["gemini-1.5-pro", "gemini-1.5-flash", "gemini-1.0-pro"],
        default_model="gemini-1.5-pro",
        description="Google Gemini API",
    ),
    "deepseek": VeyaProviderSpec(
        name="deepseek",
        display_name="DeepSeek",
        protocol_family="openai",  # OpenAI-compatible
        base_url="https://api.deepseek.com/v1",
        capabilities={"function-calling", "streaming"},
        models=["deepseek-chat", "deepseek-coder"],
        default_model="deepseek-chat",
        description="DeepSeek API",
    ),
    "openrouter": VeyaProviderSpec(
        name="openrouter",
        display_name="OpenRouter",
        protocol_family="openai",  # OpenAI-compatible
        base_url="https://openrouter.ai/api/v1",
        capabilities={"vision", "function-calling", "streaming", "image-gen"},
        models=["openrouter/auto", "anthropic/claude-3.5-sonnet", "openai/gpt-4o", "google/gemini-pro"],
        default_model="openrouter/auto",
        description="OpenRouter unified API",
    ),
    "bailian": VeyaProviderSpec(
        name="bailian",
        display_name="DashScope / Bailian",
        protocol_family="openai",  # OpenAI-compatible
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        capabilities={"vision", "function-calling", "streaming"},
        models=["qwen-max", "qwen-plus", "qwen-turbo", "qwen-vl-max"],
        default_model="qwen-max",
        description="Alibaba DashScope Bailian API",
    ),
    "kimi": VeyaProviderSpec(
        name="kimi",
        display_name="Moonshot Kimi",
        protocol_family="openai",  # OpenAI-compatible
        base_url="https://api.moonshot.cn/v1",
        capabilities={"vision", "function-calling", "streaming"},
        models=["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        default_model="moonshot-v1-128k",
        description="Moonshot Kimi API",
    ),
    "zhipu": VeyaProviderSpec(
        name="zhipu",
        display_name="Zhipu AI",
        protocol_family="openai",  # OpenAI-compatible
        base_url="https://open.bigmodel.cn/api/paas/v4",
        capabilities={"vision", "function-calling", "streaming", "image-gen"},
        models=["glm-4", "glm-4v", "glm-3-turbo", "cogview-3"],
        default_model="glm-4",
        description="Zhipu AI API",
    ),
}


# ── Model Source Configuration ──────────────────────────────────────────

@dataclass(frozen=True)
class ModelSourceConfig:
    """Configuration for a bot's model source."""
    source: str  # "server" | "user_key" | "cli"
    provider: str = ""
    model: str = ""
    api_key_ref: str | None = None  # Encrypted key reference
    cli_agent: str | None = None  # "codex", "claude", "kimi", "gemini", "custom"
    cli_args: list[str] = field(default_factory=list)

    def __post_init__(self):
        valid_sources = {"server", "user_key", "cli"}
        if self.source not in valid_sources:
            raise ValueError(f"model_source.source must be one of {valid_sources}")


# ── Veya Provider Registry ──────────────────────────────────────────────

class VeyaProviderRegistry:
    """
    Veya's unified provider registry.

    Wraps platform 3O ProviderRegistry and adds:
    - Per-user encrypted API key storage
    - Bot model source configuration
    - Builtin provider specs with capabilities
    - CLI agent detection
    """

    def __init__(self, storage_dir: Path | None = None):
        self.storage_dir = storage_dir or Path.home() / ".veya" / "providers"
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        # Platform 3O registry (single source)
        self._platform = _PlatformProviderRegistry.get() if _PlatformProviderRegistry else None

        # Veya-specific state
        self._user_keys: dict[str, str] = {}  # user_id -> {provider: encrypted_key}
        self._bot_model_sources: dict[str, ModelSourceConfig] = {}  # bot_id -> config
        self._custom_providers: dict[str, VeyaProviderSpec] = {}
        self._cli_agents: dict[str, dict[str, Any]] = {}

        self._load()

    def _load(self) -> None:
        """Load persisted state."""
        keys_file = self.storage_dir / "user_keys.json"
        if keys_file.exists():
            try:
                data = json.loads(keys_file.read_text())
                for user_id, providers in data.items():
                    self._user_keys[user_id] = {
                        p: _decrypt_value(enc) for p, enc in providers.items()
                    }
            except Exception:
                pass

        sources_file = self.storage_dir / "bot_model_sources.json"
        if sources_file.exists():
            try:
                data = json.loads(sources_file.read_text())
                for bot_id, config in data.items():
                    self._bot_model_sources[bot_id] = ModelSourceConfig(**config)
            except Exception:
                pass

        custom_file = self.storage_dir / "custom_providers.json"
        if custom_file.exists():
            try:
                data = json.loads(custom_file.read_text())
                for name, spec in data.items():
                    self._custom_providers[name] = VeyaProviderSpec(**spec)
            except Exception:
                pass

        self._detect_cli_agents()

    def _save_keys(self) -> None:
        keys_file = self.storage_dir / "user_keys.json"
        data = {
            user_id: {p: _encrypt_value(k) for p, k in providers.items()}
            for user_id, providers in self._user_keys.items()
        }
        keys_file.write_text(json.dumps(data, indent=2))

    def _save_sources(self) -> None:
        sources_file = self.storage_dir / "bot_model_sources.json"
        data = {bot_id: config.__dict__ for bot_id, config in self._bot_model_sources.items()}
        sources_file.write_text(json.dumps(data, indent=2))

    def _save_custom(self) -> None:
        custom_file = self.storage_dir / "custom_providers.json"
        data = {name: spec.__dict__ for name, spec in self._custom_providers.items()}
        custom_file.write_text(json.dumps(data, indent=2))

    # ── Builtin Provider Specs ──────────────────────────────────────────

    def get_builtin_providers(self) -> dict[str, VeyaProviderSpec]:
        """Get all builtin provider specs."""
        return dict(BUILTIN_PROVIDERS)

    def get_custom_providers(self) -> dict[str, VeyaProviderSpec]:
        """Get user-defined custom providers."""
        return dict(self._custom_providers)

    def get_all_providers(self) -> dict[str, VeyaProviderSpec]:
        """Get all providers (builtin + custom)."""
        result = dict(BUILTIN_PROVIDERS)
        result.update(self._custom_providers)
        return result

    def get_provider_spec(self, name: str) -> VeyaProviderSpec | None:
        """Get provider spec by name."""
        if name in BUILTIN_PROVIDERS:
            return BUILTIN_PROVIDERS[name]
        return self._custom_providers.get(name)

    def register_custom_provider(self, spec: VeyaProviderSpec) -> None:
        """Register a custom provider (e.g., OpenAI-compatible base_url)."""
        if spec.name in BUILTIN_PROVIDERS:
            raise ProviderRegistryError(f"Cannot override builtin provider: {spec.name}")
        self._custom_providers[spec.name] = spec
        self._save_custom()

    def unregister_custom_provider(self, name: str) -> bool:
        """Unregister a custom provider."""
        if name in self._custom_providers:
            del self._custom_providers[name]
            self._save_custom()
            return True
        return False

    # ── User API Keys ───────────────────────────────────────────────────

    def set_user_api_key(self, user_id: str, provider: str, api_key: str) -> None:
        """Store encrypted API key for a user/provider."""
        if user_id not in self._user_keys:
            self._user_keys[user_id] = {}
        self._user_keys[user_id][provider] = api_key
        self._save_keys()

    def get_user_api_key(self, user_id: str, provider: str) -> str | None:
        """Retrieve decrypted API key for a user/provider."""
        return self._user_keys.get(user_id, {}).get(provider)

    def delete_user_api_key(self, user_id: str, provider: str) -> bool:
        """Delete a user's API key for a provider."""
        if user_id in self._user_keys and provider in self._user_keys[user_id]:
            del self._user_keys[user_id][provider]
            self._save_keys()
            return True
        return False

    def list_user_providers(self, user_id: str) -> list[str]:
        """List providers for which a user has stored keys."""
        return list(self._user_keys.get(user_id, {}).keys())

    # ── Bot Model Source ────────────────────────────────────────────────

    def set_bot_model_source(self, bot_id: str, config: ModelSourceConfig) -> None:
        """Set the model source for a bot."""
        self._bot_model_sources[bot_id] = config
        self._save_sources()

    def get_bot_model_source(self, bot_id: str) -> ModelSourceConfig | None:
        """Get the model source configuration for a bot."""
        return self._bot_model_sources.get(bot_id)

    def get_effective_model_config(self, bot_id: str, user_id: str) -> dict[str, Any]:
        """
        Get the effective model configuration for a bot.

        Resolves the model source to actual provider/model/key.
        """
        config = self.get_bot_model_source(bot_id)
        if config is None:
            # Default: server default
            return {"source": "server", "provider": "", "model": "", "api_key": None}

        if config.source == "server":
            return {"source": "server", "provider": "", "model": "", "api_key": None}

        if config.source == "user_key":
            api_key = self.get_user_api_key(user_id, config.provider)
            return {
                "source": "user_key",
                "provider": config.provider,
                "model": config.model,
                "api_key": api_key,
            }

        if config.source == "cli":
            return {
                "source": "cli",
                "provider": "",
                "model": "",
                "api_key": None,
                "cli_agent": config.cli_agent,
                "cli_args": config.cli_args,
            }

        return {"source": "server", "provider": "", "model": "", "api_key": None}

    # ── CLI Agent Detection ─────────────────────────────────────────────

    def _detect_cli_agents(self) -> None:
        """Detect installed CLI agents."""
        cli_agents = {
            "codex": ["codex", "--version"],
            "claude": ["claude", "--version"],
            "gemini": ["gemini", "--version"],
            "kimi": ["kimi", "--version"],
        }
        for name, cmd in cli_agents.items():
            try:
                import subprocess
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    self._cli_agents[name] = {
                        "available": True,
                        "version": result.stdout.strip().splitlines()[0] if result.stdout else "unknown",
                        "path": cmd[0],
                    }
                else:
                    self._cli_agents[name] = {"available": False}
            except Exception:
                self._cli_agents[name] = {"available": False}

    def get_cli_agents(self) -> dict[str, dict[str, Any]]:
        """Get detected CLI agents."""
        return dict(self._cli_agents)

    def is_cli_agent_available(self, name: str) -> bool:
        """Check if a CLI agent is available."""
        return self._cli_agents.get(name, {}).get("available", False)

    # ── Platform 3O Registry Delegation ────────────────────────────────

    def register_llm(self, name: str, caller: Any, replace: bool = False) -> None:
        """Delegate to platform registry."""
        if self._platform:
            self._platform.register_llm(name, caller, replace=replace)

    def register_vlm(self, name: str, caller: Any, replace: bool = False) -> None:
        """Delegate to platform registry."""
        if self._platform:
            self._platform.register_vlm(name, caller, replace=replace)

    def register_image_gen(self, name: str, caller: Any, replace: bool = False) -> None:
        """Delegate to platform registry."""
        if self._platform:
            self._platform.register_image_gen(name, caller, replace=replace)

    def llm(self, name: str = "default") -> Any:
        """Get LLM caller from platform registry."""
        if self._platform:
            return self._platform.llm(name)
        raise ProviderNotFoundError(f"llm provider {name!r} not registered")

    def vlm(self, name: str = "default") -> Any:
        """Get VLM caller from platform registry."""
        if self._platform:
            return self._platform.vlm(name)
        raise ProviderNotFoundError(f"vlm provider {name!r} not registered")

    def image_gen(self, name: str = "default") -> Any:
        """Get ImageGen caller from platform registry."""
        if self._platform:
            return self._platform.image_gen(name)
        raise ProviderNotFoundError(f"image_gen provider {name!r} not registered")


# ── Singleton Instance ──────────────────────────────────────────────────

_VEYA_PROVIDER_REGISTRY: VeyaProviderRegistry | None = None


def get_veya_provider_registry(storage_dir: Path | None = None) -> VeyaProviderRegistry:
    """Get the singleton VeyaProviderRegistry instance."""
    global _VEYA_PROVIDER_REGISTRY
    if _VEYA_PROVIDER_REGISTRY is None:
        _VEYA_PROVIDER_REGISTRY = VeyaProviderRegistry(storage_dir)
    return _VEYA_PROVIDER_REGISTRY


__all__ = [
    "ProviderRegistryError",
    "VeyaProviderSpec",
    "ModelSourceConfig",
    "BUILTIN_PROVIDERS",
    "VeyaProviderRegistry",
    "get_veya_provider_registry",
    # Re-export platform types
    "LLMCaller",
    "VLMCaller",
    "ImageGenCaller",
    "ProviderSpec",
    "OBaseRegistryConflict",
    "ProviderNotFoundError",
    "ProviderDiscoveryError",
    "ProtocolFamily",
]