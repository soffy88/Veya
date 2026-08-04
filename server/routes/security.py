"""
Security & Audit API - Security logs, permissions, and compliance
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/security", tags=["security"])


class AuditLogEntry(BaseModel):
    timestamp: str
    user: str
    action: str
    resource: str
    success: bool
    details: dict[str, Any]


class PermissionCheck(BaseModel):
    user: str
    permission: str
    granted: bool
    context: str


@router.get("/audit-logs")
async def get_audit_logs(
    start_time: str | None = None,
    end_time: str | None = None,
    user: str | None = None,
    action: str | None = None,
    limit: int = 100,
):
    """Get security audit logs with filtering"""
    try:
        log_file = Path("security_audit.json")
        if not log_file.exists():
            return {"logs": [], "total": 0}

        logs = []
        with open(log_file) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    logs.append(entry)
                except json.JSONDecodeError:
                    continue

        # Apply filters
        filtered_logs = logs

        if start_time:
            filtered_logs = [log for log in filtered_logs if log.get("timestamp", "") >= start_time]

        if end_time:
            filtered_logs = [log for log in filtered_logs if log.get("timestamp", "") <= end_time]

        if user:
            filtered_logs = [log for log in filtered_logs if log.get("user") == user]

        if action:
            filtered_logs = [
                log for log in filtered_logs if action.lower() in log.get("action", "").lower()
            ]

        # Sort by timestamp (newest first) and limit
        filtered_logs.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        filtered_logs = filtered_logs[:limit]

        return {"logs": filtered_logs, "total": len(logs), "filtered": len(filtered_logs)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read audit logs: {e!s}")


@router.get("/audit-stats")
async def get_audit_stats():
    """Get summary statistics from audit logs"""
    try:
        log_file = Path("security_audit.json")
        if not log_file.exists():
            return {
                "total_actions": 0,
                "successful_actions": 0,
                "failed_actions": 0,
                "unique_users": 0,
                "top_actions": [],
            }

        logs = []
        with open(log_file) as f:
            for line in f:
                try:
                    logs.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue

        total = len(logs)
        successful = sum(1 for log in logs if log.get("success"))
        failed = total - successful

        users = set(log.get("user") for log in logs if log.get("user"))

        # Count actions
        action_counts = {}
        for log in logs:
            action = log.get("action", "unknown")
            action_counts[action] = action_counts.get(action, 0) + 1

        top_actions = sorted(action_counts.items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            "total_actions": total,
            "successful_actions": successful,
            "failed_actions": failed,
            "success_rate": round(successful / total * 100, 2) if total > 0 else 0,
            "unique_users": len(users),
            "users": list(users),
            "top_actions": [{"action": k, "count": v} for k, v in top_actions],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to calculate stats: {e!s}")


@router.post("/validate-input")
async def validate_input_endpoint(input_data: str, context: str = "general"):
    """Validate input against security rules"""
    try:
        from security.validator import validate_input

        is_valid = validate_input(input_data, context)
        return {"valid": is_valid, "input": input_data, "context": context}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Validation error: {e!s}")


@router.get("/permissions")
async def get_permissions_config():
    """Get current security permissions configuration"""
    try:
        from security.validator import load_security_config

        config = load_security_config()
        return {
            "default_permissions": config.get("default_permissions", []),
            "restricted_permissions": config.get("restricted_permissions", []),
            "rate_limits": config.get("rate_limits", {}),
            "whitelist": config.get("whitelist", {}),
            "blacklist": config.get("blacklist", {}),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load config: {e!s}")


@router.get("/violations")
async def get_recent_violations(limit: int = 20):
    """Get recent security violations (failed actions)"""
    try:
        log_file = Path("security_audit.json")
        if not log_file.exists():
            return {"violations": [], "total": 0}

        logs = []
        with open(log_file) as f:
            for line in f:
                try:
                    logs.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue

        violations = [log for log in logs if not log.get("success")]
        violations.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        violations = violations[:limit]

        return {
            "violations": violations,
            "total": len([log for log in logs if not log.get("success")]),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get violations: {e!s}")
