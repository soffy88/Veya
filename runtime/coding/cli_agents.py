"""CLI coding agent registry with PATH-based detection and version reporting."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("veya.cli_agents")

CLI_AGENTS_REL_PATH = ".veya/cli_agents.json"

# Built-in agent profiles: name -> (command, version_flag)
_BUILTIN_PROFILES: dict[str, tuple[str, str]] = {
    "devin": ("devin", "--version"),
    "claude-code": ("claude", "--version"),
    "codex": ("codex", "--version"),
    "cursor-agent": ("cursor-agent", "--version"),
    "gemini": ("gemini", "--version"),
    "copilot": ("copilot", "--version"),
    "qwen": ("qwen", "--version"),
    "droid": ("droid", "--version"),
    "cline": ("cline", "--version"),
    "opencode": ("opencode", "--version"),
    "grok": ("grok", "--version"),
    "hermes": ("hermes", "--version"),
}

# Runtime command specs live beside the CLI registry and are consumed by the
# existing CLIWorkerExecutorAdapter. Login checks inspect presence or exit
# status only; credential files are never opened or parsed.
XRT_RUNTIME_PROFILES: dict[str, dict[str, Any]] = {
    "devin": {"profile_name": "devin", "display_name": "Devin", "command": "devin", "headless_args": ["run", "--prompt"], "login_check_files": [".devin/credentials.json"], "env_allowlist": []},
    "claude_code": {"profile_name": "claude-code", "display_name": "Claude Code", "command": "claude", "headless_args": ["-p", "--permission-mode", "acceptEdits"], "login_check_files": [".claude.json", ".config/claude/config.json"], "env_allowlist": ["CLAUDE_CONFIG_DIR"]},
    "codex": {"profile_name": "codex", "display_name": "Codex CLI", "command": "codex", "headless_args": ["exec", "--skip-git-repo-check", "--sandbox", "workspace-write"], "login_check_files": [".codex/auth.json", ".config/codex/credentials.json"], "env_allowlist": ["CODEX_HOME"]},
    "cursor_agent": {"profile_name": "cursor-agent", "display_name": "Cursor CLI", "command": "cursor-agent", "headless_args": ["-p"], "login_check_files": [".cursor/credentials.json"], "env_allowlist": []},
    "gemini": {"profile_name": "gemini", "display_name": "Gemini CLI", "command": "gemini", "headless_args": ["-p"], "login_check_files": [".gemini/credentials.json", ".gemini/oauth_creds.json"], "env_allowlist": ["GEMINI_CLI_HOME"]},
    "copilot": {"profile_name": "copilot", "display_name": "GitHub Copilot CLI", "command": "copilot", "headless_args": ["-p"], "login_check_cmd": ["copilot", "auth", "status"], "login_check_files": [".config/gh/hosts.yml"], "env_allowlist": []},
    "qwen": {"profile_name": "qwen", "display_name": "Qwen Code", "command": "qwen", "headless_args": ["-p"], "login_check_files": [".qwen/credentials.json", ".qwen/oauth_creds.json"], "env_allowlist": ["QWEN_HOME"]},
    "droid": {"profile_name": "droid", "display_name": "Droid", "command": "droid", "headless_args": ["run"], "login_check_files": [".factory/auth.json", ".droid/config.json"], "env_allowlist": []},
    "cline": {"profile_name": "cline", "display_name": "Cline", "command": "cline", "headless_args": ["run"], "login_check_files": [".cline/data", ".cline/config.json"], "env_allowlist": []},
    "opencode": {"profile_name": "opencode", "display_name": "OpenCode", "command": "opencode", "headless_args": ["run"], "login_check_files": [".local/share/opencode/auth.json", ".config/opencode/auth.json", ".opencode/auth.json"], "env_allowlist": ["OPENCODE_CONFIG_DIR", "OPENCODE_DATA_DIR"]},
    "grok": {"profile_name": "grok", "display_name": "Grok", "command": "grok", "headless_args": ["-p"], "login_check_files": [".grok/sessions", ".config/grok/auth.json"], "env_allowlist": ["GROK_SESSION_DIR"]},
    "hermes": {"profile_name": "hermes", "display_name": "Hermes", "command": "hermes", "headless_args": ["run"], "login_check_files": [".hermes/config.json"], "env_allowlist": ["HERMES_HOME"]},
}
_XRT_BY_PROFILE = {item["profile_name"]: (runtime_id, item) for runtime_id, item in XRT_RUNTIME_PROFILES.items()}


@dataclass(frozen=True)
class CLIAgentProfile:
    """A CLI agent profile with detection info."""

    name: str
    command_template: str
    detected_path: str | None = None
    version: str | None = None
    is_custom: bool = False
    available: bool = False
    runtime_id: str | None = None
    display_name: str | None = None
    headless_args: tuple[str, ...] = ()
    login_check_files: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command_template": self.command_template,
            "detected_path": self.detected_path,
            "version": self.version,
            "is_custom": self.is_custom,
            "available": self.available,
            "runtime_id": self.runtime_id,
            "display_name": self.display_name,
            "headless_args": list(self.headless_args),
            "login_check_files": list(self.login_check_files),
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


def _run_version_check(command: str, runtime_id: str | None = None) -> str | None:
    """Run command --version and return version string."""
    try:
        result = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=safe_runtime_env(runtime_id),
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
    for name, (default_command, _version_flag) in _BUILTIN_PROFILES.items():
        runtime_info = _XRT_BY_PROFILE.get(name)
        runtime_id, metadata = runtime_info if runtime_info else (None, {})
        command = str(metadata.get("command") or default_command)
        detected_path = shutil.which(command)
        profile_fields = {
            "runtime_id": runtime_id,
            "display_name": metadata.get("display_name"),
            "headless_args": tuple(metadata.get("headless_args", ())),
            "login_check_files": tuple(metadata.get("login_check_files", ())),
        }
        if detected_path:
            version = _run_version_check(command, runtime_id)
            profiles[name] = CLIAgentProfile(
                name=name,
                command_template=f"{command} {{prompt}} --cwd {{cwd}}",
                detected_path=detected_path,
                version=version,
                is_custom=False,
                available=True,
                **profile_fields,
            )
        else:
            profiles[name] = CLIAgentProfile(
                name=name,
                command_template=f"{command} {{prompt}} --cwd {{cwd}}",
                detected_path=None,
                version=None,
                is_custom=False,
                available=False,
                **profile_fields,
            )
    return profiles


def xrt_feature_enabled() -> bool:
    """Master opt-in: disabled by default; a comma list enables a subset."""
    value = os.getenv("VEYA_XRT_RUNTIMES", "0").strip().lower()
    if value in {"", "0", "false", "no", "off", "disabled"}:
        return False
    if value in {"1", "true", "yes", "on", "all"}:
        return True
    return bool({part.strip() for part in value.split(",") if part.strip()})


def xrt_runtime_enabled(runtime_id: str) -> bool:
    value = os.getenv("VEYA_XRT_RUNTIMES", "0").strip().lower()
    if value in {"1", "true", "yes", "on", "all"}:
        default_enabled = True
    else:
        default_enabled = runtime_id.lower() in {
            part.strip() for part in value.split(",") if part.strip()
        }
    override = os.getenv(f"VEYA_XRT_RUNTIME_{runtime_id.upper()}")
    if override is not None and override.strip().lower() in {"0", "false", "no", "off"}:
        return False
    return default_enabled


def safe_runtime_env(runtime_id: str | None = None) -> dict[str, str]:
    """Build a conservative environment without provider tokens or API keys."""
    allowed = {"PATH", "HOME", "LANG", "TERM", "USER"}
    allowed.update(key for key in os.environ if key.startswith("LC_"))
    if runtime_id in XRT_RUNTIME_PROFILES:
        allowed.update(XRT_RUNTIME_PROFILES[runtime_id].get("env_allowlist", ()))
    return {key: os.environ[key] for key in sorted(allowed) if key in os.environ}


def build_xrt_command(runtime_id: str, prompt: str) -> tuple[list[str], dict[str, str]]:
    """Return argv and a safe environment for one registered XRT profile."""
    if runtime_id not in XRT_RUNTIME_PROFILES:
        raise ValueError(f"unknown XRT runtime: {runtime_id}")
    if not xrt_runtime_enabled(runtime_id):
        raise PermissionError(f"XRT runtime is disabled: {runtime_id}")
    profile = XRT_RUNTIME_PROFILES[runtime_id]
    command = str(profile["command"])
    binary = shutil.which(command)
    if binary is None:
        raise FileNotFoundError(f"XRT runtime is not installed: {runtime_id}")
    args = [str(arg) for arg in profile.get("headless_args", [])]
    return [binary, *args, prompt], safe_runtime_env(runtime_id)


def probe_xrt_runtime(runtime_id: str, repo_root: Path | str) -> dict[str, Any]:
    """Probe binary/version and login state without reading credential contents."""
    from pathlib import Path

    if runtime_id not in XRT_RUNTIME_PROFILES:
        raise ValueError(f"unknown XRT runtime: {runtime_id}")
    metadata = XRT_RUNTIME_PROFILES[runtime_id]
    registry = get_cli_agent_registry(repo_root)
    profile = registry.get(str(metadata["profile_name"]))
    installed = bool(profile and profile.detected_path)
    state = "not_installed"
    logged_in = False
    if not xrt_runtime_enabled(runtime_id):
        state = "disabled"
    elif installed:
        login_cmd = metadata.get("login_check_cmd")
        if login_cmd:
            command = str(login_cmd[0])
            binary = shutil.which(command)
            if binary:
                try:
                    result = subprocess.run(
                        [binary, *[str(part) for part in login_cmd[1:]]],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=8,
                        check=False,
                        env=safe_runtime_env(runtime_id),
                    )
                    logged_in = result.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    logged_in = False
        if not logged_in:
            home = Path(os.environ.get("HOME") or Path.home()).expanduser()
            logged_in = any((home / relative).exists() for relative in metadata.get("login_check_files", []))
        state = "ready" if logged_in else "installed_not_logged_in"
    return {
        "name": runtime_id,
        "profile_name": metadata["profile_name"],
        "display_name": metadata["display_name"],
        "state": state,
        "installed": installed,
        "version": profile.version if profile else None,
        "logged_in": logged_in,
        "path": profile.detected_path if profile else None,
    }


def probe_xrt_runtimes(repo_root: Path | str) -> list[dict[str, Any]]:
    return [probe_xrt_runtime(runtime_id, repo_root) for runtime_id in XRT_RUNTIME_PROFILES]


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
    "CLI_AGENTS_REL_PATH",
    "CLIAgentProfile",
    "CLIAgentRegistry",
    "XRT_RUNTIME_PROFILES",
    "build_xrt_command",
    "probe_xrt_runtime",
    "probe_xrt_runtimes",
    "safe_runtime_env",
    "xrt_feature_enabled",
    "xrt_runtime_enabled",
    "_validate_command_template",
    "get_cli_agent_registry",
]
