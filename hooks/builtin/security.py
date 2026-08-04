from pathlib import Path
from typing import Any

import yaml

from security.audit import audit_logger
from security.validator import validate_input


def load_security_config():
    """Load security config from YAML file"""
    config_path = Path(__file__).parent.parent.parent / "config" / "security.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


# Global security state
SECURITY_STATE = {"active_permissions": set(), "rate_limits": {}, "last_activity": {}}


def pre_security_check(data: dict[str, Any]) -> dict[str, Any]:
    """
    Pre-execution security check hook
    Validates inputs, checks permissions, enforces rate limits
    """
    user = data.get("user", "anonymous")
    action = data.get("action", "unknown")

    # Log the security check
    audit_logger.log_action(user, f"PRE_SECURITY_CHECK_{action.upper()}", "SYSTEM", True)

    # Validate inputs based on action type
    if "file_path" in data and not validate_input(data["file_path"], "file_path"):
        audit_logger.log_file_operation(user, "ACCESS", data["file_path"], False)
        raise PermissionError(f"Invalid file path: {data['file_path']}")

    if "api_endpoint" in data and not validate_input(data["api_endpoint"], "api_call"):
        audit_logger.log_api_call(user, data["api_endpoint"], "POST", False)
        raise PermissionError(f"Blocked API call to: {data['api_endpoint']}")

    # Check permissions
    required_permission = _get_required_permission(action)
    if required_permission:
        if not _check_permission(user, required_permission):
            audit_logger.log_permission_check(user, required_permission, False, action)
            raise PermissionError(f"Permission denied: {required_permission}")
        else:
            audit_logger.log_permission_check(user, required_permission, True, action)

    # Enforce rate limits
    if not _check_rate_limit(user, action):
        raise PermissionError(f"Rate limit exceeded for {user}: {action}")

    return data


def post_security_audit(data: dict[str, Any], result: Any) -> Any:
    """
    Post-execution security audit hook
    Logs successful operations and cleans up
    """
    user = data.get("user", "anonymous")
    action = data.get("action", "unknown")

    # Determine success from result
    success = result is not None and not isinstance(result, Exception)

    if "file_path" in data:
        audit_logger.log_file_operation(user, action, data["file_path"], success)
    elif "api_endpoint" in data:
        method = data.get("method", "GET")
        audit_logger.log_api_call(user, data["api_endpoint"], method, success)
    else:
        audit_logger.log_action(user, action, "SYSTEM", success)

    return result


def _get_required_permission(action: str) -> str:
    """Map actions to required permissions"""
    permission_map = {
        "write_file": "write_files",
        "execute_shell": "shell_execute",
        "call_api": "call_external_api",
        "read_sensitive": "read_sensitive_data",
    }
    return permission_map.get(action)


def _check_permission(user: str, permission: str) -> bool:
    """Check if user has required permission"""
    config = load_security_config()

    # For demo purposes, allow all default permissions
    default_perms = set(config.get("default_permissions", []))
    restricted_perms = set(config.get("restricted_permissions", []))

    if permission in default_perms:
        return True
    elif permission in restricted_perms:
        # In real implementation, check user roles/ACLs
        # For now, deny all restricted permissions
        return False

    return True


def _check_rate_limit(user: str, action: str) -> bool:
    """Check if user is within rate limits"""
    import time

    now = time.time()
    config = load_security_config()

    # Get rate limit for action type
    limits = config.get("rate_limits", {})
    limit_key = f"{action}_per_minute"
    max_count = limits.get(limit_key, 10)  # default to 10 per minute

    # Initialize user activity if not present
    if user not in SECURITY_STATE["last_activity"]:
        SECURITY_STATE["last_activity"][user] = {}

    user_actions = SECURITY_STATE["last_activity"][user]

    # Get current count and last reset time
    action_stats = user_actions.get(action, {"count": 0, "reset_time": now})

    # Reset counter if minute has passed
    if now - action_stats["reset_time"] >= 60:
        action_stats["count"] = 0
        action_stats["reset_time"] = now

    # Check if limit exceeded
    if action_stats["count"] >= max_count:
        return False

    # Increment count
    action_stats["count"] += 1
    user_actions[action] = action_stats
    SECURITY_STATE["last_activity"][user] = user_actions

    return True


# Hook definition for registry
HOOK_DEFINITIONS = {
    "pre_security_check": {
        "type": "pre_dispatch",
        "function": pre_security_check,
        "priority": 10,  # High priority - run early
    },
    "post_security_audit": {
        "type": "post_result",
        "function": post_security_audit,
        "priority": 90,  # Low priority - run late
    },
}

__all__ = ["HOOK_DEFINITIONS", "post_security_audit", "pre_security_check"]
