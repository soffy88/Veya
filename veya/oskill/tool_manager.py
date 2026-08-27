"""
veya/oskill/tool_manager.py — Universal CLI Tool Manager (Layer 2).

Discovers, downloads, verifies, and manages external CLI tools needed by
veya agents (ripgrep, Claude Code, Playwright, ffmpeg, etc.).

Supports:
- Discovery: check which tools are installed and their versions
- Download: fetch binaries from official sources
- Verify: SHA256 checksum validation
- Cache: local binary cache in ~/.veya/tools/
- Version pinning: support specific tool versions
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Tool specifications
# ---------------------------------------------------------------------------


@dataclass
class ToolSpec:
    """Specification for an external CLI tool."""

    name: str  # e.g., "ripgrep"
    cli_command: str  # e.g., "rg"
    description: str
    homepage: str = ""
    # Installation
    install_methods: list[str] = field(default_factory=list)
    # ["brew:ripgrep", "apt:ripgrep", "npm:@anthropic-ai/claude-code", "pip:codex-cli", "direct:url"]
    direct_downloads: dict[str, str] = field(default_factory=dict)
    # {"linux-x86_64": "https://...", "darwin-arm64": "https://..."}
    sha256_checksums: dict[str, str] = field(default_factory=dict)
    # {"linux-x86_64": "abc123...", ...}
    min_version: str = ""
    required_by: list[str] = field(default_factory=list)
    # ["veya.search", "veya.spawn", "veya.browser"]


# Registry of known tools
_TOOL_REGISTRY: dict[str, ToolSpec] = {
    "ripgrep": ToolSpec(
        name="ripgrep",
        cli_command="rg",
        description="Ultra-fast text search tool",
        homepage="https://github.com/BurntSushi/ripgrep",
        install_methods=["brew:ripgrep", "apt:ripgrep", "cargo:ripgrep"],
        direct_downloads={
            "linux-x86_64": "https://github.com/BurntSushi/ripgrep/releases/download/14.1.0/ripgrep-14.1.0-x86_64-unknown-linux-musl.tar.gz",
            "darwin-arm64": "https://github.com/BurntSushi/ripgrep/releases/download/14.1.0/ripgrep-14.1.0-aarch64-apple-darwin.tar.gz",
        },
        required_by=["veya.search", "veya.code_review"],
    ),
    "claude-code": ToolSpec(
        name="claude-code",
        cli_command="claude",
        description="Anthropic Claude Code CLI",
        homepage="https://github.com/anthropics/claude-code",
        install_methods=["npm:@anthropic-ai/claude-code"],
        required_by=["veya.spawn"],
    ),
    "codex": ToolSpec(
        name="codex",
        cli_command="codex",
        description="OpenAI Codex CLI",
        install_methods=["pip:codex-cli"],
        required_by=["veya.spawn"],
    ),
    "playwright": ToolSpec(
        name="playwright",
        cli_command="playwright",
        description="Browser automation framework",
        homepage="https://playwright.dev",
        install_methods=["npm:playwright", "pip:playwright"],
        required_by=["veya.browser"],
    ),
    "ffmpeg": ToolSpec(
        name="ffmpeg",
        cli_command="ffmpeg",
        description="Audio/video processing",
        homepage="https://ffmpeg.org",
        install_methods=["brew:ffmpeg", "apt:ffmpeg"],
        required_by=["veya.voice", "veya.vision"],
    ),
    "aider": ToolSpec(
        name="aider",
        cli_command="aider",
        description="AI pair programming tool",
        homepage="https://aider.chat",
        install_methods=["pip:aider-chat"],
        required_by=["veya.spawn"],
    ),
    "node": ToolSpec(
        name="node",
        cli_command="node",
        description="Node.js JavaScript runtime",
        homepage="https://nodejs.org",
        install_methods=["brew:node", "apt:nodejs"],
        required_by=["veya.spawn", "veya.browser"],
    ),
    "git": ToolSpec(
        name="git",
        cli_command="git",
        description="Version control system",
        install_methods=["brew:git", "apt:git"],
        required_by=["veya.core"],
    ),
}


# ---------------------------------------------------------------------------
# Tool Manager
# ---------------------------------------------------------------------------


class ToolManager:
    """Universal CLI tool manager — discover, download, verify, cache.

    Example:
        >>> tm = ToolManager()
        >>> status = tm.check("ripgrep")
        >>> if not status.installed:
        ...     result = tm.install("ripgrep")
    """

    def __init__(self, cache_dir: Path | None = None):
        self._cache_dir = cache_dir or Path.home() / ".veya" / "tools"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ── Discovery ──────────────────────────────────────────────────────

    def check(self, name: str) -> ToolStatus:
        """Check if a tool is installed and get its version."""
        spec = _TOOL_REGISTRY.get(name)
        if spec is None:
            return ToolStatus(name=name, installed=False, error=f"Unknown tool: {name}")

        path = shutil.which(spec.cli_command)
        if path is None:
            return ToolStatus(name=name, installed=False)

        version = self._get_version(name, spec)
        return ToolStatus(
            name=name,
            installed=True,
            path=path,
            version=version,
        )

    def check_all(self) -> list[ToolStatus]:
        """Check all registered tools."""
        return [self.check(name) for name in _TOOL_REGISTRY]

    def check_required_by(self, capability: str) -> list[ToolStatus]:
        """Check all tools required by a specific veya capability."""
        required = [name for name, spec in _TOOL_REGISTRY.items() if capability in spec.required_by]
        return [self.check(name) for name in required]

    # ── Installation ───────────────────────────────────────────────────

    def install(self, name: str, method: str = "auto") -> InstallResult:
        """Install a tool.

        Args:
            name: Tool name.
            method: Installation method ("auto", "brew", "apt", "npm", "pip", "direct").

        Returns:
            InstallResult.
        """
        spec = _TOOL_REGISTRY.get(name)
        if spec is None:
            return InstallResult(name=name, success=False, error=f"Unknown tool: {name}")

        # Check if already installed
        status = self.check(name)
        if status.installed:
            return InstallResult(
                name=name,
                success=True,
                message=f"Already installed: {spec.cli_command} v{status.version}",
            )

        # Try each install method
        methods = spec.install_methods if method == "auto" else [f"{method}:{name}"]

        for m in methods:
            result = self._try_install(name, spec, m)
            if result.success:
                return result

        return InstallResult(
            name=name,
            success=False,
            error=f"All install methods failed for {name}",
        )

    def install_all(self, names: list[str] | None = None) -> list[InstallResult]:
        """Install multiple tools."""
        targets = names or list(_TOOL_REGISTRY.keys())
        return [self.install(name) for name in targets]

    def _try_install(self, name: str, spec: ToolSpec, method: str) -> InstallResult:
        """Try a specific install method."""
        parts = method.split(":", 1)
        manager = parts[0]
        package = parts[1] if len(parts) > 1 else name

        try:
            if manager == "brew":
                return self._run_install(name, ["brew", "install", package])
            elif manager == "apt":
                return self._run_install(name, ["sudo", "apt-get", "install", "-y", package])
            elif manager == "npm":
                npm_cmd = shutil.which("npm") or shutil.which("npx") or "npm"
                return self._run_install(name, [npm_cmd, "install", "-g", package])
            elif manager == "pip":
                return self._run_install(name, ["pip", "install", package])
            elif manager == "cargo":
                return self._run_install(name, ["cargo", "install", package])
            elif manager == "direct":
                return self._install_direct(name, spec)
        except Exception as e:
            return InstallResult(name=name, success=False, error=str(e))

        return InstallResult(name=name, success=False, error=f"Unknown manager: {manager}")

    def _run_install(self, name: str, cmd: list[str]) -> InstallResult:
        """Run an install command."""
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                return InstallResult(
                    name=name,
                    success=True,
                    message=f"Installed via {' '.join(cmd[:2])}",
                )
            return InstallResult(
                name=name,
                success=False,
                error=result.stderr[:500],
            )
        except subprocess.TimeoutExpired:
            return InstallResult(name=name, success=False, error="Installation timed out")
        except FileNotFoundError:
            return InstallResult(
                name=name,
                success=False,
                error=f"Command not found: {cmd[0]}",
            )

    def _install_direct(self, name: str, spec: ToolSpec) -> InstallResult:
        """Install via direct binary download."""
        plat = self._platform_key()
        url = spec.direct_downloads.get(plat)
        if not url:
            return InstallResult(
                name=name,
                success=False,
                error=f"No direct download for platform: {plat}",
            )

        try:
            import urllib.request

            dest = self._cache_dir / spec.cli_command
            tmp = dest.with_suffix(".tmp")

            # Download
            urllib.request.urlretrieve(url, str(tmp))

            # Verify checksum
            expected_hash = spec.sha256_checksums.get(plat)
            if expected_hash:
                actual = hashlib.sha256(tmp.read_bytes()).hexdigest()
                if actual != expected_hash:
                    tmp.unlink()
                    return InstallResult(
                        name=name,
                        success=False,
                        error=f"SHA256 mismatch: expected {expected_hash[:16]}..., got {actual[:16]}...",
                    )

            # Install
            tmp.chmod(0o755)
            tmp.rename(dest)

            return InstallResult(
                name=name,
                success=True,
                message=f"Downloaded {spec.cli_command} to {dest}",
            )
        except Exception as e:
            return InstallResult(name=name, success=False, error=str(e))

    # ── Version ────────────────────────────────────────────────────────

    def _get_version(self, name: str, spec: ToolSpec) -> str:
        """Get the version of an installed tool."""
        version_flags = {
            "rg": ["--version"],
            "claude": ["--version"],
            "codex": ["--version"],
            "playwright": ["--version"],
            "ffmpeg": ["-version"],
            "aider": ["--version"],
            "node": ["--version"],
            "git": ["--version"],
        }
        flags = version_flags.get(spec.cli_command, ["--version"])
        try:
            result = subprocess.run(
                [spec.cli_command, *flags],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip().split("\n")[0][:100]
        except Exception:
            return "unknown"

    @staticmethod
    def _platform_key() -> str:
        """Get the platform key for binary downloads."""
        system = platform.system().lower()  # "linux", "darwin"
        machine = platform.machine().lower()  # "x86_64", "arm64"
        arch = "arm64" if machine == "arm64" or machine == "aarch64" else "x86_64"
        return f"{system}-{arch}"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ToolStatus:
    name: str
    installed: bool
    path: str = ""
    version: str = ""
    error: str = ""


@dataclass
class InstallResult:
    name: str
    success: bool
    message: str = ""
    error: str = ""
