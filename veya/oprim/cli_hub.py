"""CLI-Hub oprims — one atomic CLI operation each (NANO-W8, ``VEYA_CLI_HUB``).

Four atoms, each a single external effect:

* :func:`search_cli` — read the CLI-Anything registry
* :func:`read_cli_skill` — read one entry's SKILL.md
* :func:`install_cli_on_runner` — install one pinned package, on a runner
* :func:`invoke_cli` — run one CLI with structured JSON arguments

Every transport is injected (``http_get`` / ``run_cmd``) and defaults to the
obase handle, so a caller can substitute a fake and the oprim never reaches
for the network or a subprocess itself.  The flag is checked first in every
atom: with ``VEYA_CLI_HUB`` unset these raise before anything else happens.

CLI-Anything (HKUDS) is Apache-2.0 and is used here as a *protocol* only —
nothing is copied from it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from veya.obase.cli_hub import (
    cli_hub_registry_base_url,
    read_json,
    registry_http_get,
    registry_url,
)
from veya.obase.errors import VeyaError

#: A pinned distribution name: no paths, no URLs, no shell metacharacters.
_DISTRIBUTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+!_-]{0,63}$")


class CLIHubUnsupportedError(VeyaError):
    """Raised when CLI-Hub is off, unconfigured, or cannot do what was asked."""

    def __init__(self, op_name: str, reason: str):
        super().__init__(f"CLI-Hub unsupported for {op_name}: {reason}")
        self.operation = op_name
        self.reason = reason


def _require_cli_hub(op_name: str) -> None:
    if os.environ.get("VEYA_CLI_HUB", "").strip().lower() not in ("1", "true", "yes", "on"):
        raise CLIHubUnsupportedError(
            op_name, "VEYA_CLI_HUB flag not set (set VEYA_CLI_HUB=1 to enable)"
        )


def _require_registry(op_name: str) -> str:
    base_url = cli_hub_registry_base_url()
    if not base_url:
        raise CLIHubUnsupportedError(
            op_name,
            "no CLI-Hub registry configured (set VEYA_CLI_HUB_BASE_URL)",
        )
    return base_url


def _require_pinned(op_name: str, package: str, version: str) -> str:
    """A CLI install is always pinned: an unpinned runner install is a surprise
    upgrade of code the bot will later invoke."""
    if not _DISTRIBUTION_RE.match(str(package or "")):
        raise CLIHubUnsupportedError(op_name, f"invalid package name: {package!r}")
    if not _VERSION_RE.match(str(version or "")):
        raise CLIHubUnsupportedError(op_name, f"a pinned version is required, got {version!r}")
    return f"{package}=={version}"


def search_cli(
    *,
    query: str,
    http_get: Any = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Search the CLI-Hub registry.

    Args:
        query: Free-text search.
        http_get: Injected GET callable (defaults to the obase handle).
        base_url: Registry root (defaults to ``VEYA_CLI_HUB_BASE_URL``).

    Returns:
        ``{"query": str, "base_url": str, "results": [...]}``
    """
    _require_cli_hub("search_cli")
    root = base_url if base_url is not None else _require_registry("search_cli")
    getter = http_get or registry_http_get
    payload = read_json(getter(registry_url(root, "search", query={"q": str(query)})))
    results = payload.get("results")
    if results is None:
        results = payload.get("tools") or []
    return {"query": query, "base_url": root, "results": list(results)}


def read_cli_skill(
    *,
    skill_id: str,
    http_get: Any = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Read one registry entry's SKILL.md.

    Returns:
        ``{"skill_id": str, "base_url": str, "content": str}``
    """
    _require_cli_hub("read_cli_skill")
    root = base_url if base_url is not None else _require_registry("read_cli_skill")
    getter = http_get or registry_http_get
    response = getter(registry_url(root, "skills", str(skill_id)))
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        raise CLIHubUnsupportedError(
            "read_cli_skill", f"registry returned HTTP {status} for {skill_id!r}"
        )
    return {
        "skill_id": str(skill_id),
        "base_url": root,
        "content": str(getattr(response, "text", "") or ""),
    }


def install_cli_on_runner(
    *,
    package: str,
    version: str,
    run_cmd: Any = None,
) -> dict[str, Any]:
    """Install one pinned package on the runner this process runs on.

    The caller is responsible for approval and for only ever calling this on a
    runner: the server must not install into its own environment.

    Returns:
        ``{"success": bool, "package": str, "version": str, ...}``
    """
    _require_cli_hub("install_cli_on_runner")
    pinned = _require_pinned("install_cli_on_runner", package, version)
    runner = run_cmd or _default_run_cmd
    result = runner(["pip", "install", "--no-input", "--disable-pip-version-check", pinned])
    if int(getattr(result, "returncode", 1) or 0) != 0:
        return {
            "success": False,
            "package": str(package),
            "version": str(version),
            "error": str(getattr(result, "stderr", "") or "install failed"),
        }
    return {
        "success": True,
        "package": str(package),
        "version": str(version),
        "installed": pinned,
        "output": str(getattr(result, "stdout", "") or "").strip(),
    }


def invoke_cli(
    *,
    command: list[str],
    args: dict[str, Any] | None = None,
    run_cmd: Any = None,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Invoke one CLI with structured JSON arguments.

    The payload is passed as a single ``--json`` document so a CLI can be
    written against one contract instead of a flag zoo.

    Returns:
        ``{"success": bool, "command": [...], "output": str, "data": ...}``
    """
    _require_cli_hub("invoke_cli")
    cmd = [str(part) for part in (command or [])]
    if not cmd:
        raise CLIHubUnsupportedError("invoke_cli", "command is required")
    if args is not None:
        cmd.extend(["--json", json.dumps(args, ensure_ascii=False, default=str)])

    runner = run_cmd or _default_run_cmd
    result = runner(cmd, timeout=timeout_s)
    stdout = str(getattr(result, "stdout", "") or "")
    if int(getattr(result, "returncode", 1) or 0) != 0:
        return {
            "success": False,
            "command": cmd,
            "error": str(getattr(result, "stderr", "") or "invocation failed"),
        }
    parsed: Any = None
    try:
        parsed = json.loads(stdout)
    except (TypeError, ValueError):
        parsed = None
    return {
        "success": True,
        "command": cmd,
        "output": stdout,
        "data": parsed,
    }


def _default_run_cmd(cmd: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Run one command, capturing its output. No shell, no inherited streams."""

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


__all__ = [
    "CLIHubUnsupportedError",
    "install_cli_on_runner",
    "invoke_cli",
    "read_cli_skill",
    "search_cli",
]
