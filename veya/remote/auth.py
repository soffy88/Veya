"""Bearer authentication for the remote MCP gateway (spec §6).

Hard properties implemented here:

* secrets are stored only as SHA-256 digests; the raw token is never persisted
  or logged;
* verification is constant-time per candidate (``hmac.compare_digest``) and the
  loop never short-circuits on the first mismatch;
* no configured credentials means *deny everything* (fail closed);
* revocation and rotation are first-class operations.

Direct file I/O for the token store is intentional and scoped to this file:
``veya/remote`` is the credential boundary, outside the governed business
directories. ``# 3O-IO-ALLOW`` documents that exemption explicitly.
"""

# 3O-IO-ALLOW: reads the remote token store configured by the operator
# (VEYA_REMOTE_TOKENS / VEYA_REMOTE_TOKENS_FILE). This is operator credential
# loading, not business-layer I/O; the runtime never executes here.

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import RemotePermissions

_TOKEN_ENV = "VEYA_REMOTE_TOKENS"
_TOKEN_FILE_ENV = "VEYA_REMOTE_TOKENS_FILE"
_DEFAULT_TOKEN_FILE = "~/.veya/remote_tokens.json"
_BEARER_PREFIX = "bearer "


class RemoteAuthError(Exception):
    """Authentication/authorization failure. Always mapped to AUTH_DENIED."""

    code = "AUTH_DENIED"

    def __init__(self, message: str = "authentication denied") -> None:
        super().__init__(message)
        self.message = message


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass
class RemoteToken:
    """A hashed bearer credential with an explicit capability grant."""

    token_id: str
    principal: str
    secret_sha256: str
    permissions: RemotePermissions = field(default_factory=RemotePermissions)
    workspaces: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    revoked_at: float | None = None
    label: str = ""

    def is_active(self, now: float | None = None) -> bool:
        moment = now if now is not None else time.time()
        if self.revoked_at is not None:
            return False
        return not (self.expires_at is not None and moment >= self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        """Serializable form. Never contains the raw secret."""

        return {
            "token_id": self.token_id,
            "principal": self.principal,
            "secret_sha256": self.secret_sha256,
            "permissions": self.permissions.to_dict(),
            "workspaces": list(self.workspaces),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RemoteToken:
        return cls(
            token_id=str(data["token_id"]),
            principal=str(data.get("principal", "")),
            secret_sha256=str(data["secret_sha256"]),
            permissions=RemotePermissions.from_dict(data.get("permissions")),
            workspaces=tuple(str(w) for w in data.get("workspaces", ())),
            created_at=float(data.get("created_at", time.time())),
            expires_at=data.get("expires_at"),
            revoked_at=data.get("revoked_at"),
            label=str(data.get("label", "")),
        )


class RemoteAuth:
    """In-memory credential authority. Build it from env/file or explicit tokens."""

    def __init__(self, tokens: Iterable[RemoteToken] = (), *, store_path: str | Path | None = None):
        self._tokens: dict[str, RemoteToken] = {}
        self._store_path = Path(store_path).expanduser() if store_path else None
        for token in tokens:
            self._tokens[token.token_id] = token

    # ── construction ────────────────────────────────────────────────
    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> RemoteAuth:
        env = environ if environ is not None else os.environ
        raw = env.get(_TOKEN_ENV, "").strip()
        store = env.get(_TOKEN_FILE_ENV, _DEFAULT_TOKEN_FILE)
        path = Path(store).expanduser()
        if raw:
            return cls([RemoteToken.from_dict(item) for item in json.loads(raw)], store_path=path)
        tokens: list[RemoteToken] = []
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            tokens = [RemoteToken.from_dict(item) for item in data]
        return cls(tokens, store_path=path)

    @property
    def token_count(self) -> int:
        return len(self._tokens)

    def tokens(self) -> tuple[RemoteToken, ...]:
        """Snapshot of the configured tokens (never includes raw secrets)."""

        return tuple(self._tokens.values())

    @property
    def configured(self) -> bool:
        return bool(self._tokens)

    # ── issuing / rotation ──────────────────────────────────────────
    def issue(
        self,
        principal: str,
        *,
        permissions: RemotePermissions | None = None,
        workspaces: Iterable[str] = (),
        ttl_s: float | None = None,
        token_id: str | None = None,
        label: str = "",
        secret: str | None = None,
    ) -> tuple[RemoteToken, str]:
        """Create a token. Returns ``(record, raw_secret)``; secret is shown once."""

        raw_secret = secret or secrets.token_urlsafe(32)
        now = time.time()
        record = RemoteToken(
            token_id=token_id or f"rt_{secrets.token_hex(8)}",
            principal=principal,
            secret_sha256=_digest(raw_secret),
            permissions=permissions or RemotePermissions(),
            workspaces=tuple(str(w) for w in workspaces),
            created_at=now,
            expires_at=(now + ttl_s) if ttl_s else None,
            label=label,
        )
        self._tokens[record.token_id] = record
        self._persist()
        return record, raw_secret

    def revoke(self, token_id: str) -> bool:
        record = self._tokens.get(token_id)
        if record is None:
            return False
        record.revoked_at = time.time()
        self._persist()
        return True

    def rotate(self, token_id: str, *, secret: str | None = None) -> str | None:
        """Replace a token's secret. Returns the new raw secret, or None if absent."""

        record = self._tokens.get(token_id)
        if record is None:
            return None
        raw_secret = secret or secrets.token_urlsafe(32)
        record.secret_sha256 = _digest(raw_secret)
        record.revoked_at = None
        self._persist()
        return raw_secret

    def get(self, token_id: str) -> RemoteToken | None:
        return self._tokens.get(token_id)

    # ── verification ────────────────────────────────────────────────
    def verify(self, authorization: str | None) -> RemoteToken:
        """Verify a ``Authorization: Bearer <secret>`` header. Fail closed."""

        # No tokens configured -> nothing can authenticate.
        if not self._tokens:
            raise RemoteAuthError("remote access is not configured")
        if not authorization or not authorization.lower().startswith(_BEARER_PREFIX):
            raise RemoteAuthError("missing bearer token")
        presented = authorization[len(_BEARER_PREFIX) :].strip()
        if not presented:
            raise RemoteAuthError("empty bearer token")
        candidate = _digest(presented)
        now = time.time()
        matched: RemoteToken | None = None
        # Constant-time-ish: compare against every digest, no early exit.
        for record in self._tokens.values():
            if hmac.compare_digest(record.secret_sha256, candidate):
                matched = record
        if matched is None or not matched.is_active(now):
            raise RemoteAuthError("invalid or revoked token")
        return matched

    # ── persistence ─────────────────────────────────────────────────
    def _persist(self) -> None:
        if self._store_path is None:
            return
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [t.to_dict() for t in self._tokens.values()], ensure_ascii=False, indent=2
        )
        temporary = self._store_path.with_name(f".{self._store_path.name}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self._store_path)
        with contextlib.suppress(OSError):
            os.chmod(self._store_path, 0o600)
