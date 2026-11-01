"""Provider usage / quota readings for the status board (Codync method).

Quotas are read with local, read-only commands the *operator* configures --
never with a model call, never with a credential, and never by sending
anything anywhere. Each command prints one JSON object on stdout::

    {"used": 12, "limit": 100, "unit": "credits"}

Only the parsed numbers are kept; raw output is discarded, and a command
that fails, times out, or prints anything else becomes an ``error`` entry
instead of a number. No command is configured by default: with no
``VEYA_USAGE_COMMANDS`` the board simply reports ``configured: false``.

``VEYA_USAGE_COMMANDS`` is a JSON object mapping a provider name to an
argv list, e.g. ``{"claude": ["claude", "-p", "/usage", "--json"]}``.
Commands run without a shell, with a 10s timeout, and their output is
capped. The snapshot is cached under the bots root so the board -- polled
often -- never spawns a subprocess; ``POST /usage/refresh`` re-reads.
A snapshot older than :data:`USAGE_STALE_AFTER_S` is served with
``stale: true`` rather than hidden.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from server.bot_team.conversations import atomic_write_json, default_bots_root

#: Environment variable carrying the provider command map.
USAGE_COMMANDS_ENV = "VEYA_USAGE_COMMANDS"

#: Seconds a cached snapshot stays fresh.
USAGE_STALE_AFTER_S = 15 * 60

#: Hard cap on a command's captured stdout.
_MAX_OUTPUT_BYTES = 64 * 1024

#: Provider names are identifiers, not paths or shell fragments.
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

USAGE_FILE = "usage.json"


def usage_commands_from_env() -> dict[str, list[str]]:
    """Parse ``VEYA_USAGE_COMMANDS``; anything malformed yields no commands."""

    raw = os.environ.get(USAGE_COMMANDS_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    commands: dict[str, list[str]] = {}
    for provider, argv in parsed.items():
        if not isinstance(provider, str) or not _PROVIDER_RE.match(provider):
            continue
        if not isinstance(argv, list) or not argv:
            continue
        if not all(isinstance(part, str) and part for part in argv):
            continue
        commands[provider] = list(argv)
    return commands


def read_usage(
    commands: dict[str, list[str]] | None = None, *, timeout: float = 10.0
) -> dict[str, Any]:
    """Run each configured command once and return the parsed snapshot."""

    if commands is None:
        commands = usage_commands_from_env()
    providers: dict[str, Any] = {}
    for provider, argv in commands.items():
        try:
            proc = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            providers[provider] = {"error": f"command failed: {exc.__class__.__name__}"}
            continue
        if proc.returncode != 0:
            providers[provider] = {"error": f"command exited {proc.returncode}"}
            continue
        try:
            payload = json.loads(proc.stdout[:_MAX_OUTPUT_BYTES].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            providers[provider] = {"error": "unparseable output (expected one JSON object)"}
            continue
        if not isinstance(payload, dict):
            providers[provider] = {"error": "unparseable output (expected one JSON object)"}
            continue
        used = payload.get("used")
        limit = payload.get("limit")
        if not isinstance(used, (int, float)) or not isinstance(limit, (int, float)):
            providers[provider] = {"error": "output lacks numeric used/limit"}
            continue
        entry: dict[str, Any] = {"used": used, "limit": limit}
        if isinstance(payload.get("unit"), str) and payload["unit"]:
            entry["unit"] = payload["unit"]
        if limit:
            entry["remaining"] = limit - used
        providers[provider] = entry
    return {
        "configured": bool(commands),
        "providers": providers,
        "updated_ts": time.time(),
        "stale": False,
    }


def usage_path(root: str | Path | None = None) -> Path:
    return Path(root).expanduser() if root is not None else default_bots_root()


def usage_snapshot(root: str | Path | None = None) -> dict[str, Any]:
    """The cached snapshot; missing or stale cache is reported, not hidden."""

    path = usage_path(root) / USAGE_FILE
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"configured": False, "providers": {}, "updated_ts": None, "stale": False}
    if not isinstance(cached, dict):
        return {"configured": False, "providers": {}, "updated_ts": None, "stale": False}
    updated = cached.get("updated_ts")
    stale = bool(
        isinstance(updated, (int, float)) and time.time() - updated > USAGE_STALE_AFTER_S
    )
    return {**cached, "stale": stale}


def refresh_usage(
    commands: dict[str, list[str]] | None = None,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Re-read every configured command and cache the snapshot."""

    snapshot = read_usage(commands)
    base = usage_path(root)
    base.mkdir(parents=True, exist_ok=True)
    atomic_write_json(base / USAGE_FILE, snapshot)
    return snapshot


__all__ = [
    "USAGE_COMMANDS_ENV",
    "USAGE_STALE_AFTER_S",
    "read_usage",
    "refresh_usage",
    "usage_commands_from_env",
    "usage_path",
    "usage_snapshot",
]
