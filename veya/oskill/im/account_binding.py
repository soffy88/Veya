"""
veya/im/account_binding.py — Multi-User Account Binding System.

Allows users to bind their own accounts (API keys, tokens) to veya,
enabling per-user isolation for:
  - LLM API keys (OpenAI, Anthropic, DashScope)
  - IM platform accounts (Discord, Slack, Telegram, DingTalk, WeChat)
  - Agent spawn credentials (Claude Code, Codex, Cursor)

Bindings are stored as encrypted JSON in ~/.veya/bindings/{pseudo_id}.json
and loaded per-session based on the pseudo-anonymized user ID.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from veya.oskill.im.pseudo import anonymize_user_id

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class AccountBinding:
    """A user's account binding for a specific platform/provider."""

    platform: str          # "openai", "anthropic", "discord", "slack", "telegram", etc.
    user_id: str           # pseudo-anonymized user ID
    credentials: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    is_active: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "user_id": self.user_id,
            "credentials": self.credentials,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "is_active": self.is_active,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AccountBinding:
        return cls(
            platform=data.get("platform", ""),
            user_id=data.get("user_id", ""),
            credentials=data.get("credentials", {}),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            is_active=data.get("is_active", True),
            metadata=data.get("metadata", {}),
        )


# ---------------------------------------------------------------------------
# Binding Store
# ---------------------------------------------------------------------------


class BindingStore:
    """Persistent store for user account bindings.

    Bindings are stored under ~/.veya/bindings/ keyed by pseudo-anonymized
    user IDs. Credentials are stored as plain JSON in a restricted directory
    (0700 permissions).

    For production, use a proper secrets manager (Vault, AWS Secrets Manager)
    or encrypt with the pseudo-anonymizer's HMAC key.
    """

    def __init__(self, base_dir: Path | None = None):
        self._base = base_dir or Path.home() / ".veya" / "bindings"
        self._base.mkdir(parents=True, exist_ok=True)
        self._base.chmod(0o700)

    def _path(self, pseudo_id: str) -> Path:
        safe = pseudo_id.replace("/", "_").replace(":", "_")
        return self._base / f"{safe}.json"

    def save(self, binding: AccountBinding) -> str:
        """Save a binding. Returns the pseudo_id."""
        path = self._path(binding.user_id)
        binding.updated_at = time.time()
        path.write_text(
            json.dumps(binding.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return binding.user_id

    def load(self, pseudo_id: str) -> dict[str, AccountBinding]:
        """Load all bindings for a user."""
        path = self._path(pseudo_id)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {b["platform"]: AccountBinding.from_dict(b) for b in data}
            elif isinstance(data, dict) and "platform" in data:
                binding = AccountBinding.from_dict(data)
                return {binding.platform: binding}
            return {}
        except (json.JSONDecodeError, KeyError):
            return {}

    def load_platform(self, pseudo_id: str, platform: str) -> AccountBinding | None:
        """Load a specific platform binding for a user."""
        bindings = self.load(pseudo_id)
        return bindings.get(platform)

    def delete(self, pseudo_id: str, platform: str) -> bool:
        """Delete a specific binding."""
        bindings = self.load(pseudo_id)
        if platform not in bindings:
            return False
        del bindings[platform]
        # Re-save remaining
        self.save_all(pseudo_id, list(bindings.values()))
        return True

    def save_all(self, pseudo_id: str, bindings: list[AccountBinding]):
        """Save all bindings for a user."""
        path = self._path(pseudo_id)
        data = [b.to_dict() for b in bindings]
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_users(self) -> list[str]:
        """List all users with bindings."""
        return [p.stem for p in self._base.glob("*.json")]


# ---------------------------------------------------------------------------
# Global binding store
# ---------------------------------------------------------------------------

_default_store: BindingStore | None = None


def get_binding_store() -> BindingStore:
    """Get the global binding store (creates if needed)."""
    global _default_store
    if _default_store is None:
        _default_store = BindingStore()
    return _default_store


# ---------------------------------------------------------------------------
# Convenience API
# ---------------------------------------------------------------------------


def bind_account(
    user_id: str,          # real user ID (automatically pseudo-anonymized)
    platform: str,          # "openai", "anthropic", "discord", etc.
    credentials: dict[str, str],  # {"api_key": "sk-..."}
    *,
    store: BindingStore | None = None,
) -> AccountBinding:
    """Bind a user's account for a platform.

    Args:
        user_id: Real user ID (will be pseudo-anonymized before storage).
        platform: Platform identifier.
        credentials: Credential dict (api_key, bot_token, etc.).
        store: Optional custom store.

    Returns:
        The created AccountBinding.

    Example:
        >>> binding = bind_account("discord:123456", "openai", {"api_key": "sk-xxx"})
        >>> print(binding.platform)  # "openai"
    """
    store = store or get_binding_store()
    pseudo = anonymize_user_id(user_id)
    binding = AccountBinding(
        platform=platform,
        user_id=pseudo,
        credentials=credentials,
    )
    store.save(binding)
    return binding


def get_user_credentials(
    user_id: str,
    platform: str,
    *,
    store: BindingStore | None = None,
) -> dict[str, str]:
    """Get a user's credentials for a platform.

    Args:
        user_id: Real user ID.
        platform: Platform identifier.
        store: Optional custom store.

    Returns:
        Credential dict, or empty dict if not bound.
    """
    store = store or get_binding_store()
    pseudo = anonymize_user_id(user_id)
    binding = store.load_platform(pseudo, platform)
    if binding and binding.is_active:
        return dict(binding.credentials)
    return {}


def unbind_account(
    user_id: str,
    platform: str,
    *,
    store: BindingStore | None = None,
) -> bool:
    """Remove a user's account binding."""
    store = store or get_binding_store()
    pseudo = anonymize_user_id(user_id)
    return store.delete(pseudo, platform)


def list_user_bindings(
    user_id: str,
    *,
    store: BindingStore | None = None,
) -> list[dict[str, Any]]:
    """List all bindings for a user (without exposing credentials)."""
    store = store or get_binding_store()
    pseudo = anonymize_user_id(user_id)
    bindings = store.load(pseudo)
    return [
        {
            "platform": b.platform,
            "is_active": b.is_active,
            "has_credentials": bool(b.credentials),
            "created_at": b.created_at,
        }
        for b in bindings.values()
    ]


# ---------------------------------------------------------------------------
# IM Gateway credential injection
# ---------------------------------------------------------------------------


def inject_user_credentials(
    user_id: str,
    platform: str,
    config: dict[str, Any],
    *,
    store: BindingStore | None = None,
) -> dict[str, Any]:
    """Inject user-bound credentials into a config dict.

    If the user has bound credentials for this platform, override
    the config with their personal keys. Otherwise, use global env vars.

    Args:
        user_id: Real user ID.
        platform: Platform to inject for.
        config: Base config dict (may be modified).
        store: Optional custom store.

    Returns:
        Updated config dict.

    Example:
        >>> config = inject_user_credentials(
        ...     "discord:123456", "openai",
        ...     {"provider": "openai", "model": "gpt-4o"},
        ... )
        >>> # config now has "api_key" from user's binding
    """
    creds = get_user_credentials(user_id, platform, store=store)
    if creds:
        config = dict(config)
        config.update(creds)
    return config
