"""CLI coding agent registry with PATH-based detection and version reporting."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("veya.cli_agents")

CLI_AGENTS_REL_PATH = ".veya/cli_agents.json"

# Built-in agent profiles: name -> (command, version_flag)
_BUILTIN_PROFILES: dict[str, tuple[str, str]] = {
    "claude-code": ("claude", "--version"),
    "codex": ("codex", "--version"),
    "opencode": ("opencode", "--version"),
    "cursor-agent": ("cursor-agent", "--version"),
    "gemini": ("gemini", "--version"),
    "qwen": ("qwen", "--version"),
}


@dataclass(frozen=True)
class CLIAgentProfile:
    """A CLI agent profile with detection info."""

    name: str
    command_template: str
    detected_path: str | None = None
    version: str | None = None
    is_custom: bool = False
    available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command_template": self.command_template,
            "detected_path": self.detected_path,
            "version": self.version,
            "is_custom": self.is_custom,
            "available": self.available,
        }


def _validate_command_template(template: str) -> str:
    """Validate command template contains required placeholders."""
    if not isinstance(template, str) or not template.strip():
        raise ValueError("command_template must be a non-empty string")
    if "{prompt}" not in template:
        raise ValueError("command_template must contain {prompt} placeholder")
    if "{cwd}" not in template:
        raise ValueError("command_template must contain {cwd} placeholder")
    return template.strip()


def _run_version_check(command: str) -> str | None:
    """Run command --version and return version string."""
    try:
        result = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            output = result.stdout.strip()
            # Take first line as version
            return output.splitlines()[0] if output else None
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
        pass
    return None


def _discover_builtin() -> dict[str, CLIAgentProfile]:
    """Discover built-in CLI agents via PATH lookup."""
    profiles = {}
    for name, (command, version_flag) in _BUILTIN_PROFILES.items():
        detected_path = shutil.which(command)
        if detected_path:
            version = _run_version_check(command)
            profiles[name] = CLIAgentProfile(
                name=name,
                command_template=f"{command} {{prompt}} --cwd {{cwd}}",
                detected_path=detected_path,
                version=version,
                is_custom=False,
                available=True,
            )
        else:
            profiles[name] = CLIAgentProfile(
                name=name,
                command_template=f"{command} {{prompt}} --cwd {{cwd}}",
                detected_path=None,
                version=None,
                is_custom=False,
                available=False,
            )
    return profiles


def _load_custom_profiles(repo_root: Path) -> dict[str, CLIAgentProfile]:
    """Load custom profiles from .veya/cli_agents.json."""
    path = (repo_root / CLI_AGENTS_REL_PATH).resolve()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        profiles = {}
        for item in data.get("profiles", []):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            template = item.get("command_template")
            if name and template:
                profiles[name] = CLIAgentProfile(
                    name=name,
                    command_template=template,
                    detected_path=None,
                    version=None,
                    is_custom=True,
                    available=True,  # Custom profiles are always "available" (user vouches for them)
                )
        return profiles
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        logger.warning("Failed to load custom CLI agent profiles: %s", exc)
        return {}


def _save_custom_profiles(repo_root: Path, profiles: dict[str, CLIAgentProfile]) -> None:
    """Save custom profiles to .veya/cli_agents.json atomically."""
    path = (repo_root / CLI_AGENTS_REL_PATH).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    custom = {name: p.to_dict() for name, p in profiles.items() if p.is_custom}
    data = {"profiles": list(custom.values())}
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


class CLIAgentRegistry:
    """Registry of CLI agent profiles with discovery and custom registration."""

    def __init__(self, repo_root: Path | str):
        self.repo_root = Path(repo_root).expanduser().resolve()
        self._cache: dict[str, CLIAgentProfile] | None = None

    def _refresh(self) -> None:
        """Refresh the profile cache from discovery and custom storage."""
        builtin = _discover_builtin()
        custom = _load_custom_profiles(self.repo_root)
        # Custom profiles override built-in with same name
        merged = {**builtin, **custom}
        self._cache = merged

    def list_all(self) -> list[CLIAgentProfile]:
        """Return all profiles (built-in + custom)."""
        if self._cache is None:
            self._refresh()
        assert self._cache is not None
        return sorted(self._cache.values(), key=lambda p: (p.is_custom, p.name))

    def get(self, name: str) -> CLIAgentProfile | None:
        """Get a profile by name."""
        if self._cache is None:
            self._refresh()
        assert self._cache is not None
        return self._cache.get(name)

    def register_custom(self, name: str, command_template: str) -> CLIAgentProfile:
        """Register a custom CLI agent profile."""
        _validate_command_template(command_template)
        if name in _BUILTIN_PROFILES:
            raise ValueError(f"cannot override built-in profile: {name}")
        if self._cache is None:
            self._refresh()
        assert self._cache is not None
        if name in self._cache and not self._cache[name].is_custom:
            raise ValueError(f"profile {name!r} exists and is not custom")

        profile = CLIAgentProfile(
            name=name,
            command_template=command_template,
            is_custom=True,
            available=True,
        )
        custom = {k: v for k, v in self._cache.items() if v.is_custom}
        custom[name] = profile
        _save_custom_profiles(self.repo_root, custom)
        self._refresh()
        return profile

    def render_command(self, profile: CLIAgentProfile, prompt: str, cwd: str) -> list[str]:
        """Render the command template with prompt and cwd."""
        template = profile.command_template
        rendered = template.replace("{prompt}", prompt).replace("{cwd}", cwd)
        # Split into argv, respecting quoted strings
        import shlex

        return shlex.split(rendered)


def get_cli_agent_registry(repo_root: Path | str) -> CLIAgentRegistry:
    """Factory for CLI agent registry."""
    return CLIAgentRegistry(repo_root)


__all__ = [
    "CLIAgentProfile",
    "CLIAgentRegistry",
    "CLI_AGENTS_REL_PATH",
    "get_cli_agent_registry",
    "_validate_command_template",
]
