"""TDX publication policy backed by Veya's durable data directory and event log.

This is a projection/policy seam only: it delegates persistence to the existing
atomic JSON helper, auditing to the canonical event store, and interception to
the existing ToolGuard. It does not register new model-callable tools.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from server.bot_team.conversations import atomic_write_json
from server.events import append_canonical_event
from server.feature_flags import enabled as flag_enabled

log = logging.getLogger(__name__)
ACTIONS = ("push", "merge", "tag", "deploy")
_DEFAULT_PERMS = {action: False for action in ACTIONS}
_LOCK = threading.RLock()
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")


class PublishPermissionStoreError(RuntimeError):
    """Durable permission state cannot be read or written safely."""


def _store_path() -> Path:
    raw = os.getenv("VEYA_TDX_PUBLISH_PERMS_PATH") or str(
        Path.home() / ".veya" / "tdx" / "publish-perms.json"
    )
    return Path(raw).expanduser()


def _normalize_permissions(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        raise PublishPermissionStoreError("Invalid publish permissions document")
    return {action: value.get(action) is True for action in ACTIONS}


def load_permissions() -> dict[str, dict[str, bool]]:
    """Read the authoritative on-disk permission map; absent means deny all."""
    path = _store_path()
    with _LOCK:
        if not path.exists():
            return {"default": dict(_DEFAULT_PERMS)}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PublishPermissionStoreError("Cannot read TDX publish permissions") from exc
        if not isinstance(value, dict):
            raise PublishPermissionStoreError("Invalid TDX publish permissions document")
        result: dict[str, dict[str, bool]] = {"default": dict(_DEFAULT_PERMS)}
        for agent_id, permissions in value.items():
            if agent_id == "default":
                result["default"] = _normalize_permissions(permissions)
            elif isinstance(agent_id, str) and _AGENT_ID_RE.fullmatch(agent_id):
                result[agent_id] = _normalize_permissions(permissions)
        return result


def save_permissions(agent_id: str, permissions: dict[str, Any]) -> dict[str, bool]:
    """Persist one agent policy atomically, retaining all other agent policies."""
    if agent_id == "default" or not _AGENT_ID_RE.fullmatch(agent_id or ""):
        raise ValueError("agent_id must be a stable, non-default identifier")
    normalized = _normalize_permissions(permissions)
    path = _store_path()
    with _LOCK:
        existing = load_permissions()
        existing[agent_id] = normalized
        try:
            atomic_write_json(path, existing)
        except OSError as exc:
            log.exception("Unable to persist TDX publish permissions at %s", path)
            raise PublishPermissionStoreError("Cannot persist TDX publish permissions") from exc
    return normalized


def _audit(topic: str, payload: dict[str, Any], *, actor: str) -> None:
    """Append an audit fact to the canonical Veya event store; never swallow IO errors."""
    try:
        append_canonical_event(topic, payload, actor=actor)
    except Exception:
        log.exception("Could not persist TDX publication audit event %s", topic)
        raise


def check_publish_permission(agent_id: str, action: str) -> None:
    """Check an agent's publication action and persist allow/deny evidence."""
    if not flag_enabled("VEYA_TDX_AGENT_PUBLISH_PERMS"):
        return
    if action not in ACTIONS:
        raise ValueError(f"Unsupported publication action: {action}")

    try:
        permissions = load_permissions()
    except Exception as exc:
        try:
            _audit(
                "tdx.publish_denied",
                {"agent_id": agent_id, "action": action, "reason": "permission_store_unavailable"},
                actor=agent_id or "tdx-policy",
            )
        except Exception:
            log.exception("Fail-closed publication denial could not be audited")
        raise PermissionError("Publication denied: TDX permission store unavailable") from exc

    allowed = bool((permissions.get(agent_id) or permissions["default"]).get(action, False))
    topic = "tdx.publish_allowed" if allowed else "tdx.publish_denied"
    _audit(topic, {"agent_id": agent_id, "action": action}, actor=agent_id or "tdx-policy")
    if not allowed:
        raise PermissionError(
            f"Agent {agent_id!r} is not authorized by TDX publish permissions to perform {action!r}."
        )


def _strings(value: Any, depth: int = 0):
    if depth > 4:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child, depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child, depth + 1)


def classify_publish_action(tool_name: str, kwargs: dict[str, Any]) -> str | None:
    """Recognize publish operations by tool name and command arguments."""
    name = re.sub(r"[.\-\s]+", "_", (tool_name or "").lower())
    if name in {"git_push", "push", "push_branch", "push_to_remote", "remote_push", "git_promote"}:
        return "push"
    if name in {"git_merge", "merge", "merge_branch", "merge_pull_request", "pull_request_merge"}:
        return "merge"
    if name in {"git_tag", "tag", "create_tag", "git_create_tag"}:
        return "tag"
    if name in {"deploy", "deploy_service", "deploy_agent", "service_restart", "restart_service", "systemd_restart", "docker_deploy"} or name.startswith("deploy_") or name.endswith("_deploy"):
        return "deploy"

    strings = "\n".join(_strings(kwargs))
    if re.search(r"\bgit\s+(?:-C\s+\S+\s+)?push\b", strings, re.IGNORECASE):
        return "push"
    if re.search(r"\bgit\s+(?:-C\s+\S+\s+)?merge\b", strings, re.IGNORECASE):
        return "merge"
    if re.search(r"\bgit\s+(?:-C\s+\S+\s+)?tag\b", strings, re.IGNORECASE):
        return "tag"
    if re.search(r"\b(?:kubectl\s+apply|helm\s+upgrade|vercel\s+deploy|netlify\s+deploy|docker\s+compose\s+up|systemctl\s+restart)\b", strings, re.IGNORECASE):
        return "deploy"
    return None


def _current_request_is_owner() -> bool:
    """Trust only the repository auth context's explicit owner decision."""
    try:
        from server.auth import current_user

        user = current_user()
    except Exception:
        return False
    if not isinstance(user, dict):
        return False
    configured_owner = (os.getenv("VEYA_OWNER_USER_ID") or os.getenv("VEYA_TAILNET_OWNER_USER_ID") or "").strip()
    user_id = str(user.get("user_id") or "")
    return user.get("is_owner") is True or bool(configured_owner and user_id == configured_owner)


def tool_guard_policy(tool_name: str, kwargs: dict[str, Any], source: str) -> str | None:
    """Existing ToolGuard adapter: non-empty return value truly blocks dispatch."""
    action = classify_publish_action(tool_name, kwargs)
    if action is None or not flag_enabled("VEYA_TDX_AGENT_PUBLISH_PERMS"):
        return None

    if _current_request_is_owner():
        try:
            _audit(
                "tdx.publish_owner_bypass",
                {"tool_name": tool_name, "action": action, "source": source},
                actor="owner",
            )
        except Exception:
            return "Publication denied because the owner-action audit could not be persisted."
        return None

    agent_id = next(
        (str(kwargs[key]) for key in ("agent_id", "bot_id", "caller_agent_id", "executor_id") if kwargs.get(key)),
        "",
    )
    if not agent_id and source.startswith(("agent:", "bot:")):
        agent_id = source.split(":", 1)[1].strip()
    if not agent_id:
        agent_id = "default"
    try:
        check_publish_permission(agent_id, action)
    except Exception as exc:
        log.warning("TDX ToolGuard blocked %s for %s: %s", tool_name, agent_id, exc)
        return f"TDX publication policy blocked {tool_name}: {exc}"
    return None
