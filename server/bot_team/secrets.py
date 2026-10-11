"""Per-bot secrets store and runner env injector (NANO-W10 Addendum Q).

Adheres to 3O paradigm and frozen architecture decisions:
- Reuses the EXISTING ``obase.secrets_store.SecretsStore`` (no new vault).
- Secrets can be requested via tool in direct bot chats only (refused in groups: SECRETS_NOT_IN_GROUP).
- User sets value and picks scope: 'this bot' or 'all bots'.
- Injected into runner shell env as VEYA_SECRET_<NAME>.
- Exact values are masked with [secret:<NAME>] across tool results and prompts.
- Listing secrets returns metadata only, NEVER secret values.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from server.bot_team.conversations import atomic_write_json, default_bots_root
from veya.oskill.pure.secret_masking import secret_env_name
from veya.platform import obase as _load_obase

_obase = _load_obase()

_DEFAULT_VAULT_DIR = str(Path.home() / ".veya" / "vault")
_SECRETS_META_FILE = "bot_secrets.json"

SECRETS_NOT_IN_GROUP = "SECRETS_NOT_IN_GROUP"


@dataclass
class BotSecretMeta:
    """Metadata of a bot secret (never holds the secret value)."""

    name: str
    purpose: str
    requesting_bot_id: str
    scope: str = "this bot"  # "this bot" | "all bots"
    created_at: float = 0.0
    last_used_at: float = 0.0
    status: str = "requested"  # "requested" | "active"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BotSecretMeta:
        return cls(
            name=str(data.get("name") or "").strip(),
            purpose=str(data.get("purpose") or "").strip(),
            requesting_bot_id=str(data.get("requesting_bot_id") or "").strip(),
            scope=str(data.get("scope") or "this bot").strip(),
            created_at=float(data.get("created_at") or 0.0),
            last_used_at=float(data.get("last_used_at") or 0.0),
            status=str(data.get("status") or "requested").strip(),
        )


class BotSecretStore:
    """Service layer adapter coordinating metadata and the existing SecretsStore."""

    def __init__(
        self,
        root: str | Path | None = None,
        vault_dir: str | Path | None = None,
    ):
        self.root = Path(root) if root is not None else default_bots_root()
        self.meta_path = self.root / _SECRETS_META_FILE
        v_dir = Path(vault_dir or os.environ.get("VEYA_VAULT_DIR", _DEFAULT_VAULT_DIR)).expanduser()
        self._vault = _obase.secrets_store.SecretsStore(vault_dir=v_dir)

    def _load_meta(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _save_meta(self, items: dict[str, dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.meta_path, items)

    def request_secret(
        self,
        name: str,
        purpose: str,
        requesting_bot_id: str,
        *,
        is_group: bool = False,
        conversation_id: str = "",
    ) -> dict[str, Any]:
        """Record a secret request card. Refuses if initiated in a group chat."""
        if is_group:
            raise ValueError(f"{SECRETS_NOT_IN_GROUP}: secrets cannot be requested in a group chat")

        s_name = str(name or "").strip()
        if not s_name:
            raise ValueError("secret name is required")

        meta = BotSecretMeta(
            name=s_name,
            purpose=str(purpose or "").strip(),
            requesting_bot_id=str(requesting_bot_id or "").strip(),
            scope="this bot",
            created_at=time.time(),
            status="requested",
        )
        items = self._load_meta()
        items[s_name] = meta.to_dict()
        self._save_meta(items)

        card = {
            "type": "secret_request_card",
            "name": s_name,
            "purpose": meta.purpose,
            "requesting_bot_id": meta.requesting_bot_id,
            "created_at": meta.created_at,
            "conversation_id": conversation_id,
        }
        return card

    def set_secret_value(
        self,
        name: str,
        value: str,
        *,
        scope: str = "this bot",
        requesting_bot_id: str = "",
    ) -> BotSecretMeta:
        """Store the secret value in the existing vault and mark metadata active."""
        s_name = str(name or "").strip()
        if not s_name:
            raise ValueError("secret name is required")
        if not value:
            raise ValueError("secret value is required")

        valid_scope = "all bots" if scope == "all bots" else "this bot"
        vault_key = f"bot_secret:{s_name}"
        self._vault.set_secret(vault_key, str(value))

        items = self._load_meta()
        existing = items.get(s_name)
        if existing:
            meta = BotSecretMeta.from_dict(existing)
            meta.scope = valid_scope
            meta.status = "active"
            if requesting_bot_id:
                meta.requesting_bot_id = requesting_bot_id
        else:
            meta = BotSecretMeta(
                name=s_name,
                purpose="",
                requesting_bot_id=requesting_bot_id,
                scope=valid_scope,
                created_at=time.time(),
                status="active",
            )
        items[s_name] = meta.to_dict()
        self._save_meta(items)
        return meta

    def get_secret_value(self, name: str) -> str | None:
        """Retrieve value from vault (internal only; never returned by APIs/tools)."""
        s_name = str(name or "").strip()
        vault_key = f"bot_secret:{s_name}"
        if not self._vault.has_secret(vault_key):
            return None
        val = self._vault.get_secret(vault_key)
        # Update last_used_at
        items = self._load_meta()
        if s_name in items:
            items[s_name]["last_used_at"] = time.time()
            self._save_meta(items)
        return val

    def update_scope(self, name: str, scope: str) -> BotSecretMeta | None:
        """Update scope between 'this bot' and 'all bots'."""
        s_name = str(name or "").strip()
        items = self._load_meta()
        if s_name not in items:
            return None
        meta = BotSecretMeta.from_dict(items[s_name])
        meta.scope = "all bots" if scope == "all bots" else "this bot"
        items[s_name] = meta.to_dict()
        self._save_meta(items)
        return meta

    def delete_secret(self, name: str) -> bool:
        """Delete secret value and metadata."""
        s_name = str(name or "").strip()
        vault_key = f"bot_secret:{s_name}"
        if self._vault.has_secret(vault_key):
            self._vault.delete_secret(vault_key)
        items = self._load_meta()
        if s_name in items:
            del items[s_name]
            self._save_meta(items)
            return True
        return False

    def list_secrets(self, requesting_bot_id: str | None = None) -> list[dict[str, Any]]:
        """List secrets metadata (without secret values)."""
        items = self._load_meta()
        records: list[dict[str, Any]] = []
        for raw in items.values():
            meta = BotSecretMeta.from_dict(raw)
            if (
                requesting_bot_id
                and meta.scope == "this bot"
                and meta.requesting_bot_id != requesting_bot_id
            ):
                continue
            records.append(meta.to_dict())
        return records

    def get_runner_env(self, bot_id: str) -> dict[str, str]:
        """Derive runner shell env mapping {VEYA_SECRET_<NAME>: value} for this bot."""
        bid = str(bot_id or "").strip()
        items = self._load_meta()
        env: dict[str, str] = {}
        for s_name, raw in items.items():
            meta = BotSecretMeta.from_dict(raw)
            if meta.status != "active":
                continue
            if meta.scope == "all bots" or (
                meta.scope == "this bot" and meta.requesting_bot_id == bid
            ):
                val = self.get_secret_value(s_name)
                if val:
                    env[secret_env_name(s_name)] = val
        return env

    def get_masking_map(self) -> dict[str, str]:
        """Return {name: value} for all active secrets for output masking."""
        items = self._load_meta()
        mask_map: dict[str, str] = {}
        for s_name, raw in items.items():
            meta = BotSecretMeta.from_dict(raw)
            if meta.status != "active":
                continue
            vault_key = f"bot_secret:{s_name}"
            if self._vault.has_secret(vault_key):
                val = self._vault.get_secret(vault_key)
                if val:
                    mask_map[s_name] = val
        return mask_map


# Module-level default instance
_default_bot_secret_store: BotSecretStore | None = None


def bot_secret_store() -> BotSecretStore:
    global _default_bot_secret_store
    if _default_bot_secret_store is None:
        _default_bot_secret_store = BotSecretStore()
    return _default_bot_secret_store


def register_tools(registry: Any) -> None:
    """Register request_secret and list_secrets tools."""
    from server.goal_run.bot_identity import current_bot_id

    async def _request_secret_tool(
        name: str,
        purpose: str,
        is_group: bool = False,
        conversation_id: str = "",
    ) -> str:
        bot_id = current_bot_id() or "bot"
        store = bot_secret_store()
        try:
            store.request_secret(
                name=name,
                purpose=purpose,
                requesting_bot_id=bot_id,
                is_group=is_group,
                conversation_id=conversation_id,
            )
            return (
                f"Secret request card created for '{name}'. "
                "The user will provide the value in your direct chat."
            )
        except ValueError as exc:
            return f"Error: {exc}"

    async def _list_secrets_tool() -> str:
        bot_id = current_bot_id()
        store = bot_secret_store()
        secrets = store.list_secrets(requesting_bot_id=bot_id)
        return json.dumps(secrets, ensure_ascii=False)

    registry.register(
        name="request_secret",
        description=(
            "Request a secret/credential from the user (such as an API token or password). "
            "A secret card will be presented to the user in your direct chat (never in a group). "
            "The secret value will be encrypted in the secure vault and injected into your runner "
            "shell environment as VEYA_SECRET_<NAME>. Exact values will be masked from tool outputs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Identifier for the secret, e.g. 'GITHUB_TOKEN' or 'OPENAI_API_KEY'.",
                },
                "purpose": {
                    "type": "string",
                    "description": "Explanation of why this secret is required.",
                },
                "is_group": {
                    "type": "boolean",
                    "description": "Set to true if this request is originating in a group chat (will be refused).",
                },
                "conversation_id": {
                    "type": "string",
                    "description": "Optional conversation ID for the direct chat.",
                },
            },
            "required": ["name", "purpose"],
        },
        func=_request_secret_tool,
    )

    registry.register(
        name="list_secrets",
        description=(
            "List secrets available to this bot (name, purpose, requesting bot, scope, last used). "
            "Returns metadata only; never returns secret values."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        func=_list_secrets_tool,
    )


__all__ = [
    "SECRETS_NOT_IN_GROUP",
    "BotSecretMeta",
    "BotSecretStore",
    "bot_secret_store",
    "register_tools",
]
