"""Quota meter for per-backend/account usage tracking."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("veya.quota")


@dataclass(frozen=True)
class QuotaInfo:
    """Quota information for a backend/account."""

    backend: str
    account: str = "unknown"
    used: str | int = "unknown"
    limit: str | int = "unknown"
    reset_at: str = "unknown"
    unit: str = "unknown"  # tokens, requests, unknown

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "account": self.account,
            "used": self.used,
            "limit": self.limit,
            "reset_at": self.reset_at,
            "unit": self.unit,
        }


# Known CLI status output parsers
_CLI_PARSERS: dict[str, Callable[[str], QuotaInfo | None]] = {}


def _register_parser(backend_name: str):
    def decorator(func):
        _CLI_PARSERS[backend_name] = func
        return func

    return decorator


def _parse_generic_key_value(output: str) -> dict[str, str] | None:
    """Parse generic key: value lines from CLI output."""
    result = {}
    for line in output.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        result[key.strip().lower()] = value.strip()
    return result if result else None


@_register_parser("claude-code")
@_register_parser("claude")
def _parse_claude_status(output: str) -> QuotaInfo | None:
    """Parse claude-code status output."""
    data = _parse_generic_key_value(output)
    if not data:
        return None
    return QuotaInfo(
        backend="claude-code",
        account=data.get("account", "unknown"),
        used=data.get("used", "unknown"),
        limit=data.get("limit", "unknown"),
        reset_at=data.get("reset", "unknown"),
        unit=data.get("unit", "tokens"),
    )


@_register_parser("codex")
def _parse_codex_status(output: str) -> QuotaInfo | None:
    """Parse codex status output."""
    data = _parse_generic_key_value(output)
    if not data:
        return None
    return QuotaInfo(
        backend="codex",
        account=data.get("account", "unknown"),
        used=data.get("used", "unknown"),
        limit=data.get("limit", "unknown"),
        reset_at=data.get("reset_at", "unknown"),
        unit=data.get("unit", "requests"),
    )


@_register_parser("opencode")
def _parse_opencode_status(output: str) -> QuotaInfo | None:
    """Parse opencode status output."""
    data = _parse_generic_key_value(output)
    if not data:
        return None
    return QuotaInfo(
        backend="opencode",
        account=data.get("account", "unknown"),
        used=data.get("used", "unknown"),
        limit=data.get("limit", "unknown"),
        reset_at=data.get("reset", "unknown"),
        unit=data.get("unit", "tokens"),
    )


def _parse_response_headers(backend: str, headers: dict[str, str]) -> QuotaInfo | None:
    """Parse quota from HTTP response headers."""
    # Standard rate limit headers
    used = headers.get("x-ratelimit-used") or headers.get("ratelimit-used")
    limit = headers.get("x-ratelimit-limit") or headers.get("ratelimit-limit")
    reset = headers.get("x-ratelimit-reset") or headers.get("ratelimit-reset")

    if used is None and limit is None and reset is None:
        return None

    reset_at = "unknown"
    if reset:
        try:
            ts = int(reset)
            reset_at = datetime.fromtimestamp(ts, UTC).isoformat()
        except (ValueError, OSError):
            reset_at = reset

    return QuotaInfo(
        backend=backend,
        account="api",
        used=used if used is not None else "unknown",
        limit=limit if limit is not None else "unknown",
        reset_at=reset_at,
        unit="requests",
    )


class QuotaMeter:
    """Meter for collecting quota information from CLI tools and API responses."""

    def __init__(self, repo_root: Path | str):
        self.repo_root = Path(repo_root).expanduser().resolve()

    def parse_cli_status(self, backend_name: str, cli_output: str) -> QuotaInfo | None:
        """Parse quota from CLI status output."""
        parser = _CLI_PARSERS.get(backend_name.lower())
        if parser:
            try:
                return parser(cli_output)
            except Exception as exc:
                logger.warning("Failed to parse quota for %s: %s", backend_name, exc)
        return None

    def parse_response_headers(
        self, backend_name: str, headers: dict[str, str]
    ) -> QuotaInfo | None:
        """Parse quota from HTTP response headers."""
        try:
            return _parse_response_headers(backend_name, headers)
        except Exception as exc:
            logger.warning("Failed to parse quota headers for %s: %s", backend_name, exc)
        return None

    def get_quota(self, backend_name: str) -> QuotaInfo:
        """Get quota info for a backend. Returns 'unknown' for unavailable data."""
        # Try to run CLI status command
        cli_commands = {
            "claude-code": ["claude", "status"],
            "claude": ["claude", "status"],
            "codex": ["codex", "status"],
            "opencode": ["opencode", "status"],
            "gemini": ["gemini", "status"],
            "qwen": ["qwen", "status"],
            "cursor-agent": ["cursor-agent", "status"],
        }

        cmd = cli_commands.get(backend_name.lower())
        if cmd:
            import subprocess

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                    cwd=self.repo_root,
                )
                if result.returncode == 0:
                    parsed = self.parse_cli_status(backend_name, result.stdout)
                    if parsed:
                        return parsed
            except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
                pass

        # Fall back to unknown
        return QuotaInfo(backend=backend_name)

    def get_all_quotas(self, backend_names: list[str]) -> list[QuotaInfo]:
        """Get quota info for multiple backends."""
        return [self.get_quota(name) for name in backend_names]


def get_quota_meter(repo_root: Path | str) -> QuotaMeter:
    """Factory for QuotaMeter."""
    return QuotaMeter(repo_root)


__all__ = [
    "QuotaInfo",
    "QuotaMeter",
    "get_quota_meter",
]
