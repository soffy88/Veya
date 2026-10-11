"""CLI-Hub oprims for NANO-W8.

Atomic operations for CLI-Hub integration: search, read skills, install, invoke.
"""

from __future__ import annotations

import os
import json
from typing import Any, Callable

from veya.obase.errors import VeyaError


class CLIHubUnsupportedError(VeyaError):
    """Raised when CLI-Hub capability is not available."""
    def __init__(self, op_name: str, reason: str):
        super().__init__(f"CLI-Hub unsupported for {op_name}: {reason}")


def search_cli(*, query: str, http_get: Callable[[str], Any]) -> dict[str, Any]:
    """Search CLI-Hub registry for tools matching query."""
    if not os.environ.get("VEYA_CLI_HUB", "").strip().lower() in ("1", "true", "yes", "on"):
        raise CLIHubUnsupportedError("search_cli", "VEYA_CLI_HUB flag not set")
    
    response = http_get(f"https://hkuds.github.io/CLI-Anything/search?q={query}")
    return response.json()


def read_cli_skill(*, skill_id: str, http_get: Callable[[str], Any]) -> dict[str, Any]:
    """Read a skill file from CLI-Hub registry."""
    if not os.environ.get("VEYA_CLI_HUB", "").strip().lower() in ("1", "true", "yes", "on"):
        raise CLIHubUnsupportedError("read_cli_skill", "VEYA_CLI_HUB flag not set")
    
    response = http_get(f"https://hkuds.github.io/CLI-Anything/skills/{skill_id}")
    return {"content": response.text}


def install_cli_on_runner(*, package: str, version: str, run_cmd: Callable[[list[str]], Any]) -> dict[str, Any]:
    """Install a CLI package on the runner."""
    if not os.environ.get("VEYA_CLI_HUB", "").strip().lower() in ("1", "true", "yes", "on"):
        raise CLIHubUnsupportedError("install_cli_on_runner", "VEYA_CLI_HUB flag not set")
    
    result = run_cmd(["pip", "install", f"{package}=={version}"])
    if result.returncode == 0:
        return {"success": True, "already_installed": False}
    return {"success": False, "error": result.stderr}


def invoke_cli(*, command: list[str], args: dict[str, Any] | None = None, run_cmd: Callable[[list[str]], Any]) -> dict[str, Any]:
    """Invoke a CLI command with optional JSON args."""
    if not os.environ.get("VEYA_CLI_HUB", "").strip().lower() in ("1", "true", "yes", "on"):
        raise CLIHubUnsupportedError("invoke_cli", "VEYA_CLI_HUB flag not set")
    
    cmd = command[:]
    if args:
        cmd.extend(["--json", json.dumps(args)])
    
    result = run_cmd(cmd)
    if result.returncode == 0:
        return {"success": True, "output": result.stdout}
    return {"success": False, "error": result.stderr}

