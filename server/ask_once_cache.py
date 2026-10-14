"""W17: Session-scoped ask-once approval cache.

同一会话同一工具+同一参数指纹用户批准过一次后，本会话内不再询问；
TTL = 会话 TTL；
destructive/service-control 类永不缓存；
flag VEYA_APPROVAL_ASK_ONCE 默认 OFF。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any

from server.config_protection import is_protected_config_path

FLAG_NAME_APPROVAL_ASK_ONCE = "VEYA_APPROVAL_ASK_ONCE"
DEFAULT_SESSION_TTL = 3600.0

_DESTRUCTIVE_MARKERS = frozenset(
    {
        "delete",
        "destroy",
        "drop",
        "truncate",
        "kill",
        "rm",
        "purge",
        "format",
        "shutdown",
        "rmdir",
        "unlink",
    }
)

_SERVICE_CONTROL_MARKERS = frozenset(
    {
        "service_install",
        "service_uninstall",
        "service_control",
        "systemctl",
        "service_unit",
        "runner_service",
        "daemon",
    }
)


def is_ask_once_enabled() -> bool:
    """Return whether the ask-once cache flag is ON (default OFF)."""
    val = os.environ.get(FLAG_NAME_APPROVAL_ASK_ONCE, "0").strip().lower()
    return val in ("1", "true", "yes")


def get_session_ttl() -> float:
    try:
        return float(os.environ.get("VEYA_SESSION_TTL_SECONDS", str(DEFAULT_SESSION_TTL)))
    except (TypeError, ValueError):
        return DEFAULT_SESSION_TTL


def fingerprint_arguments(args: Any) -> str:
    """Stable sha256 fingerprint of the arguments dict."""
    if not isinstance(args, dict):
        args = {"_raw": str(args)}
    blob = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def is_never_cached(
    tool_or_action: str,
    effect: str = "",
    arguments: dict[str, Any] | None = None,
) -> bool:
    """True for destructive, service-control, or protected-config actions (never cached)."""
    eff = str(effect or "").strip().lower()
    if eff in ("destructive", "service_control", "service-control"):
        return True

    name = str(tool_or_action or "").strip().lower()

    # Destructive markers
    if any(marker in name for marker in _DESTRUCTIVE_MARKERS):
        return True

    # Service-control markers
    if any(marker in name for marker in _SERVICE_CONTROL_MARKERS):
        return True

    try:
        from server.permission_profiles import _R4_DESTRUCTIVE

        if name in _R4_DESTRUCTIVE:
            return True
    except Exception:
        pass

    # Protected configuration targets must never be cached (each write reviewed)
    if arguments and isinstance(arguments, dict):
        for k in ("path", "filepath", "target", "dest", "file"):
            val = arguments.get(k)
            if val and is_protected_config_path(val):
                return True

    return False


class AskOnceCache:
    """Thread-safe session-scoped cache for approved actions."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    def check(
        self,
        session_id: str,
        tool_or_action: str,
        arguments: dict[str, Any] | None,
        *,
        effect: str = "",
    ) -> dict[str, Any] | None:
        """Return cached approval entry if valid, else None."""
        if not is_ask_once_enabled() or not session_id:
            return None
        args = arguments or {}
        if is_never_cached(tool_or_action, effect=effect, arguments=args):
            return None

        fp = fingerprint_arguments(args)
        key = (str(session_id), str(tool_or_action), fp)
        ttl = get_session_ttl()
        now = time.time()

        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if now - entry["approved_at"] > ttl:
                del self._cache[key]
                return None
            return dict(entry)

    def record(
        self,
        session_id: str,
        tool_or_action: str,
        arguments: dict[str, Any] | None,
        *,
        decision: str = "approved",
        approval_id: str = "",
        effect: str = "",
    ) -> bool:
        """Cache an approval record for this session."""
        if not is_ask_once_enabled() or not session_id:
            return False
        args = arguments or {}
        if is_never_cached(tool_or_action, effect=effect, arguments=args):
            return False

        fp = fingerprint_arguments(args)
        key = (str(session_id), str(tool_or_action), fp)

        with self._lock:
            self._cache[key] = {
                "approved_at": time.time(),
                "decision": decision,
                "approval_id": approval_id,
            }
            return True

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            prefix = str(session_id)
            keys_to_del = [k for k in self._cache if k[0] == prefix]
            for k in keys_to_del:
                del self._cache[k]

    def clear_all(self) -> None:
        with self._lock:
            self._cache.clear()


_GLOBAL_CACHE = AskOnceCache()


def get_ask_once_cache() -> AskOnceCache:
    return _GLOBAL_CACHE


__all__ = [
    "DEFAULT_SESSION_TTL",
    "FLAG_NAME_APPROVAL_ASK_ONCE",
    "AskOnceCache",
    "fingerprint_arguments",
    "get_ask_once_cache",
    "get_session_ttl",
    "is_ask_once_enabled",
    "is_never_cached",
]
