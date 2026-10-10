"""server/channels/bindings.py — Channel binding table (Layer 4, persistent).

Maps ``(platform, external_chat_id)`` to the Veya target that should answer
that chat, plus the owning Veya user.

Storage discipline mirrors ``server.bot_team.push.SubscriptionStore`` — a
single JSON file, tolerant load, ``threading.Lock`` around read-modify-write,
and ``atomic_write_json`` for the commit — because a binding table is a
mutable shared record. (``veya.oskill.im.account_binding.BindingStore`` is
keyed per user with no merge, so it cannot hold this table.)

Ownership note: the store records ``owner_ref``, the stable non-PII reference
produced by ``veya.oskill.im.pseudo.anonymize_user_id``. Real Veya user ids
stay in the service layer that owns them (§5.5.1).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from server.bot_team.conversations import atomic_write_json

_log = logging.getLogger(__name__)

_BINDINGS_FILE = "channel_bindings.json"


def default_channels_root() -> Path:
    """Root directory for channel state (env-overridable, never in repo)."""
    raw = os.environ.get("VEYA_CHANNELS_ROOT") or str(Path.home() / ".veya" / "channels")
    return Path(raw).expanduser()


class ChannelBindingStore:
    """Per-channel bindings in one JSON file."""

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = Path(root) if root is not None else None
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else default_channels_root()

    @property
    def path(self) -> Path:
        return self.root / _BINDINGS_FILE

    # ---- storage --------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        items = raw.get("bindings")
        if not isinstance(items, dict):
            return {}
        return {str(k): v for k, v in items.items() if isinstance(v, dict)}

    def _save(self, items: dict[str, dict[str, Any]]) -> None:
        atomic_write_json(self.path, {"bindings": items})

    # ---- reads ----------------------------------------------------------

    def list(self, *, owner_ref: str | None = None) -> list[dict[str, Any]]:
        """All bindings, optionally only those owned by ``owner_ref``."""
        with self._lock:
            items = list(self._load().values())
        if owner_ref is None:
            return items
        return [b for b in items if b.get("owner_ref") == owner_ref]

    def get(self, binding_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._load().get(binding_id)

    def find_for_chat(self, *, platform: str, external_chat_id: str) -> dict[str, Any] | None:
        """Resolve the binding that owns an inbound external chat."""
        with self._lock:
            for binding in self._load().values():
                if binding.get("platform") == platform and binding.get("external_chat_id") == str(
                    external_chat_id
                ):
                    return binding
        return None

    # ---- writes ---------------------------------------------------------

    def bind(
        self,
        *,
        platform: str,
        external_chat_id: str,
        external_user_id: str,
        owner_ref: str,
        target_type: str,
        target_id: str,
    ) -> dict[str, Any]:
        """Create (or replace) the binding for an external chat."""
        binding_id = str(uuid.uuid4())
        record = {
            "binding_id": binding_id,
            "platform": platform,
            "external_chat_id": str(external_chat_id),
            "external_user_id": str(external_user_id),
            "owner_ref": owner_ref,
            "target_type": target_type,
            "target_id": str(target_id),
            "created_at": time.time(),
        }
        with self._lock:
            items = self._load()
            items[binding_id] = record
            self._save(items)
        return record

    def unbind(self, binding_id: str, *, owner_ref: str) -> bool:
        """Remove a binding. Returns False when absent or not owned."""
        with self._lock:
            items = self._load()
            record = items.get(binding_id)
            if record is None or record.get("owner_ref") != owner_ref:
                return False
            del items[binding_id]
            self._save(items)
        return True

    def targets_for_owner(self, owner_ref: str) -> list[dict[str, Any]]:
        """Every binding owned by ``owner_ref`` (used for feed delivery)."""
        return self.list(owner_ref=owner_ref)


_store: ChannelBindingStore | None = None
_store_lock = threading.Lock()


def binding_store() -> ChannelBindingStore:
    """Process-wide binding store."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ChannelBindingStore()
    return _store


def reset_binding_store() -> None:
    """Drop the cached store (tests)."""
    global _store
    with _store_lock:
        _store = None


__all__ = [
    "ChannelBindingStore",
    "binding_store",
    "default_channels_root",
    "reset_binding_store",
]
