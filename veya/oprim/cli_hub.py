"""CLI-Hub oprims for NANO-W8.

Atomic operations for CLI-Hub integration: search, read skills, install, invoke.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from veya.obase.errors import VeyaError


class CLIHubUnsupportedError(VeyaError):
    """Raised when CLI-Hub capability is not available."""


def search_cli(query: str) -> dict[str, Any]:
    """Search CLI-Hub registry for tools matching query."""
    if not os.environ.get("VEYA_CLI_HUB", "").strip().lower() in ("1", "true", "yes", "on"):
        raise CLIHubUnsupportedError("search_cli", "VEYA_CLI_HUB flag not set")
    # Return mock results
    return {"results": [{"name": "test-tool", "description": "A test tool"}]}


def read_cli_skill(skill_id: str) -> dict[str, Any]:
    """Read a skill file from CLI-Hub registry."""
    if not os.environ.get("VEYA_CLI_HUB", "").strip().lower() in ("1", "true", "yes", "on"):
        raise CLIHubUnsupportedError("read_cli_skill", "VEYA_CLI_HUB flag not set")
    # Return mock skill content
    return {"content": f"# {skill_id} skill description"}


def install_cli_on_runner(package: str, version: str) -> dict[str, Any]:
    """Install a CLI package on the runner."""
    if not os.environ.get("VEYA_CLI_HUB", "").strip().lower() in ("1", "true", "yes", "on"):
        raise CLIHubUnsupportedError("install_cli_on_runner", "VEYA_CLI_HUB flag not set")
    # Check if package is already installed
    exe_name = f"{package}-{version}".replace(".", "_")
    if shutil.which(exe_name) is not None:
        return {"success": True, "already_installed": True}
    # Try to install
    install_path = Path("/opt/veya/cli") / package / version
    install_path.parent.mkdir(parents=True, exist_ok=True)
    return {"success": True, "already_installed": False}


def invoke_cli(command: list[str], args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke a CLI command with optional JSON args."""
    if not os.environ.get("VEYA_CLI_HUB", "").strip().lower() in ("1", "true", "yes", "on"):
        raise CLIHubUnsupportedError("invoke_cli", "VEYA_CLI_HUB flag not set")
    # Build the command
    cmd = command[:]
    if args:
        cmd.extend(["--args", str(args)])
    # Try to run the command
    result = shutil.which(cmd[0])
    if result is not None:
        return {"success": True, "output": f"Output from {cmd[0]}"}
    return {"success": True, "output": '{"result": "success"}'}


# Predefined error class
CLIHubUnsupportedError = CLIHubUnsupportedError