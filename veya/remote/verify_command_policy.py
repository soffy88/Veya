"""Fail-closed policy for server-owned verification commands."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping


def configured_verify_commands(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    env = environ if environ is not None else os.environ
    raw = str(env.get("VEYA_VERIFY_COMMAND_ALLOWLIST", "") or "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        parsed = raw
    if isinstance(parsed, str):
        values = [parsed]
    elif isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
        values = parsed
    else:
        return ()
    result: list[str] = []
    for value in values:
        command = value.strip()
        if command and command not in result:
            result.append(command)
    return tuple(result)


def select_server_verify_command(
    requested: str | None,
    *,
    owner_authorized: bool,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return only an allowlisted command, and only for a trusted owner caller.

    If multiple commands are configured, a trusted caller must select one
    explicitly. Callers which must not select commands should pass None and
    will only get a default when exactly one command is configured.
    """
    if not owner_authorized:
        return None
    allowlist = configured_verify_commands(environ)
    if requested is None or requested == "":
        return allowlist[0] if len(allowlist) == 1 else None
    if not isinstance(requested, str) or requested not in allowlist:
        return None
    return requested
