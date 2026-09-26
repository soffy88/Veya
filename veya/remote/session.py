"""Remote session lifecycle: token -> session -> bound workspace (spec §1/§3).

Sessions are the only place where a remote client's identity, permission grant
and workspace scope are joined. They are TTL-bound, optionally capped in
number, and can be reconnected to by id (a dropped HTTP connection does not
end the session or its background jobs).

A session is a short-lived RPC context, not a durable execution owner.  The
default is therefore *no artificial admission cap*: an MCP request must never
be rejected because some durable execution is still running.
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

_UNLIMITED_CAP_TOKENS = {"", "0", "-1", "none", "null", "unlimited", "inf", "infinity"}


def normalize_session_cap(value: Any) -> int | None:
    """Return a positive hard cap, or ``None`` for unlimited admission.

    ``None``, ``0``, a negative value and the words ``none``/``unlimited`` all
    mean "no artificial global session cap".  A huge sentinel such as
    ``999999`` is *not* treated as unlimited; it stays a real (if silly) cap.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _UNLIMITED_CAP_TOKENS:
            return None
        try:
            value = int(text)
        except ValueError:
            return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class RemoteSessionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_workspace(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def workspace_is_authorized(
    workspace: str | Path, allowed_roots: tuple[str, ...] | list[str]
) -> bool:
    """Return True when workspace is an authorized root or a descendant."""

    requested = Path(canonical_workspace(workspace))
    for item in allowed_roots:
        root = Path(canonical_workspace(item))
        if requested == root or root in requested.parents:
            return True
    return False


class RemoteSessionManager:
    """In-memory registry of active remote sessions."""

    def __init__(
        self,
        *,
        ttl_s: float = 3600.0,
        max_sessions: int | None = None,
        default_workspace: str | Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_s = max(30.0, float(ttl_s))
        # ``None`` (the default) means unlimited admission.  Only an explicit
        # positive integer installs a hard cap, and that cap is an operator
        # safety valve, never an execution scheduler.
        self._max_sessions = normalize_session_cap(max_sessions)
        self._default_workspace = (
            canonical_workspace(default_workspace) if default_workspace is not None else None
        )
        self._clock = clock
        self._sessions: dict[str, RemoteSession] = {}
        self._lock = threading.Lock()

    @property
    def ttl_s(self) -> float:
        return self._ttl_s

    @property
    def max_sessions(self) -> int | None:
        """Explicit hard cap, or ``None`` when session admission is unlimited."""

        return self._max_sessions

    @property
    def default_workspace(self) -> str | None:
        """Configured default for new sessions, if the deployment supplies one."""

        return self._default_workspace

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
            # Deployment policy is authoritative for the default.  Falling back
            # to the first token entry is retained only for embedded/test
            # managers that do not provide a configured default; it prevents an
            # old token ordering from silently selecting the wrong repository in
            # the production gateway.
            active = self._default_workspace or allowed[0]
            if not workspace_is_authorized(active, list(allowed)):
                raise RemoteSessionError(
                    "WORKSPACE_DENIED",
                    f"configured default workspace is not authorized: {active}",
                )
        else:
            requested = canonical_workspace(workspace)
            if not workspace_is_authorized(requested, list(allowed)):
                raise RemoteSessionError(
                    "WORKSPACE_DENIED",
                    f"workspace is not authorized for this principal: {requested}",
                )
            if self._default_workspace is not None and not workspace_is_authorized(
                requested, (self._default_workspace,)
            ):
                raise RemoteSessionError(
                    "WORKSPACE_DENIED",
                    f"workspace is outside configured root: {requested}",
                )
            active = requested
        now = self._clock()
        with self._lock:
            self._reap_locked(now)
            # A credential identifies a principal, not a conversation.  Two
            # clients may share both credential and workspace.  Reconnection
            # must use the explicit MCP session id (see gateway.initialize).
            #
            # The cap is only enforced when an operator explicitly configured
            # one; ``None`` means new RPC contexts are never rejected because
            # of how many sessions (or executions) are already live.
            if self._max_sessions is not None and len(self._sessions) >= self._max_sessions:
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
            session.expires_at = now + self._ttl_s
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
        if not workspace_is_authorized(requested, list(session.workspaces)):
            raise RemoteSessionError(
                "WORKSPACE_DENIED", f"workspace switch not authorized: {requested}"
            )
        if self._default_workspace is not None and not workspace_is_authorized(
            requested, (self._default_workspace,)
        ):
            raise RemoteSessionError(
                "WORKSPACE_DENIED",
                f"workspace is outside configured root: {requested}",
            )
        session.active_workspace = requested
        session.explicit_workspace = requested
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
