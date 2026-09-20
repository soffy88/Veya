"""Remote session lifecycle: token -> session -> bound workspace (spec §1/§3).

Sessions are the only place where a remote client's identity, permission grant
and workspace scope are joined. They are TTL-bound, capped in number and can
be reconnected to by id (a dropped HTTP connection does not end the session or
its background jobs).
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .auth import RemoteToken
from .models import RemotePermissions, RemoteSession


class RemoteSessionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_workspace(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


class RemoteSessionManager:
    """In-memory registry of active remote sessions."""

    def __init__(
        self,
        *,
        ttl_s: float = 3600.0,
        max_sessions: int = 8,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_s = max(30.0, float(ttl_s))
        self._max_sessions = max(1, int(max_sessions))
        self._clock = clock
        self._sessions: dict[str, RemoteSession] = {}
        self._lock = threading.Lock()

    @property
    def ttl_s(self) -> float:
        return self._ttl_s

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    # ── lifecycle ───────────────────────────────────────────────────
    def create(
        self,
        token: RemoteToken,
        *,
        workspace: str | None = None,
        client_info: dict[str, Any] | None = None,
    ) -> RemoteSession:
        allowed = tuple(canonical_workspace(w) for w in token.workspaces)
        if not allowed:
            raise RemoteSessionError(
                "WORKSPACE_DENIED", "token has no authorized workspaces (fail closed)"
            )
        if workspace is None:
            active = allowed[0]
        else:
            requested = canonical_workspace(workspace)
            if requested not in allowed:
                raise RemoteSessionError(
                    "WORKSPACE_DENIED",
                    f"workspace is not authorized for this principal: {requested}",
                )
            active = requested
        now = self._clock()
        with self._lock:
            self._reap_locked(now)
            # Clients such as the OpenAI Secure MCP Tunnel re-initialize before
            # each request. Reuse the live session for the same token+workspace
            # so the cap is not exhausted and the isolated worktree mapping (and
            # any pending mutation) survives across calls.
            for existing in self._sessions.values():
                if (
                    existing.token_id == token.token_id
                    and existing.active_workspace == active
                    and not existing.is_expired(now)
                ):
                    existing.expires_at = now + self._ttl_s
                    return existing
            if len(self._sessions) >= self._max_sessions:
                raise RemoteSessionError("LIMIT_EXCEEDED", "concurrent session limit reached")
            session = RemoteSession(
                session_id=f"rs_{secrets.token_hex(12)}",
                principal=token.principal,
                token_id=token.token_id,
                workspaces=allowed,
                active_workspace=active,
                permissions=RemotePermissions.from_dict(token.permissions.to_dict()),
                created_at=now,
                expires_at=now + self._ttl_s,
                client_info=dict(client_info or {}),
            )
            self._sessions[session.session_id] = session
        return session

    def require(self, session_id: str | None) -> RemoteSession:
        if not session_id:
            raise RemoteSessionError("AUTH_DENIED", "missing session_id")
        now = self._clock()
        with self._lock:
            self._reap_locked(now)
            session = self._sessions.get(session_id)
            if session is None:
                raise RemoteSessionError("NOT_FOUND", "unknown or expired session")
            return session

    def get(self, session_id: str) -> RemoteSession | None:
        try:
            return self.require(session_id)
        except RemoteSessionError:
            return None

    def reconnect(self, session_id: str) -> RemoteSession:
        """Return an existing live session, if any (client reconnect path)."""

        return self.require(session_id)

    def close(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def active_count(self) -> int:
        now = self._clock()
        with self._lock:
            self._reap_locked(now)
            return len(self._sessions)

    def revoke_token_sessions(self, token_id: str) -> int:
        with self._lock:
            doomed = [sid for sid, s in self._sessions.items() if s.token_id == token_id]
            for sid in doomed:
                self._sessions.pop(sid, None)
            return len(doomed)

    # ── workspace binding ───────────────────────────────────────────
    def bind_workspace(self, session: RemoteSession, workspace: str) -> str:
        requested = canonical_workspace(workspace)
        if requested not in session.workspaces:
            raise RemoteSessionError(
                "WORKSPACE_DENIED", f"workspace switch not authorized: {requested}"
            )
        session.active_workspace = requested
        return requested

    def bind_worktree(self, session: RemoteSession, workspace: str, worktree_path: str) -> None:
        session.worktrees[canonical_workspace(workspace)] = str(Path(worktree_path).resolve())

    def worktree_for(self, session: RemoteSession, workspace: str) -> str | None:
        return session.worktrees.get(canonical_workspace(workspace))

    # ── internals ───────────────────────────────────────────────────
    def _reap_locked(self, now: float) -> None:
        expired = [sid for sid, s in self._sessions.items() if s.is_expired(now)]
        for sid in expired:
            self._sessions.pop(sid, None)
