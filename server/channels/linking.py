"""server/channels/linking.py — One-time channel linking codes (Layer 4).

Flow: the owner generates a short-lived code in Veya, sends it inside the
external chat, and the inbound webhook consumes it to complete the binding.

Codes are single-use, expire, and live only in memory: they are bearer
credentials for the binding operation, so they are never written to disk.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)

#: How long a generated code stays claimable.
CODE_TTL_SECONDS = 300


class CodeStore:
    """In-memory, single-use, expiring code store."""

    def __init__(self, *, ttl_seconds: int = CODE_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._codes: dict[str, dict[str, Any]] = {}

    def issue(self, *, payload: dict[str, Any]) -> str:
        """Mint a code bound to ``payload`` and return it."""
        code = secrets.token_urlsafe(16)
        with self._lock:
            self._codes[code] = {
                "payload": dict(payload),
                "expires_at": time.time() + self._ttl,
            }
        return code

    def claim(self, code: str) -> dict[str, Any] | None:
        """Consume ``code``; returns None when unknown or expired."""
        now = time.time()
        with self._lock:
            entry = self._codes.pop(code, None)
            if entry is None:
                return None
            if entry["expires_at"] < now:
                return None
            payload: dict[str, Any] = entry["payload"]
            return payload

    def purge_expired(self) -> int:
        """Drop expired codes; returns how many were removed."""
        now = time.time()
        with self._lock:
            stale = [c for c, e in self._codes.items() if e["expires_at"] < now]
            for code in stale:
                del self._codes[code]
        return len(stale)

    def __len__(self) -> int:
        with self._lock:
            return len(self._codes)


@dataclass
class LinkRequest:
    """A pending link, as issued from the settings UI."""

    platform: str
    target_type: str
    target_id: str
    owner_ref: str
    external_chat_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


_codes = CodeStore()


def codes() -> CodeStore:
    """Process-wide code store."""
    return _codes


def issue_code(request: LinkRequest) -> dict[str, Any]:
    """Issue a linking code for ``request``."""
    code = _codes.issue(
        payload={
            "platform": request.platform,
            "target_type": request.target_type,
            "target_id": request.target_id,
            "owner_ref": request.owner_ref,
            "external_chat_id": request.external_chat_id,
            "metadata": dict(request.metadata),
        }
    )
    return {"code": code, "expires_in": CODE_TTL_SECONDS}


def claim_code(code: str) -> dict[str, Any] | None:
    """Consume a linking code."""
    return _codes.claim(code)


__all__ = [
    "CODE_TTL_SECONDS",
    "CodeStore",
    "LinkRequest",
    "claim_code",
    "codes",
    "issue_code",
]
