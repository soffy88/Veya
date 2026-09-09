# 3O-IO-ALLOW hicode managed runtime boundary: this Veya adapter is the single
# injected process/filesystem boundary for the external Reasonix executable.
"""Veya-owned boundary for the managed Reasonix runtime.

HiCode is the Veya product name for this coding-executor integration.  Reasonix
is the separately licensed MIT runtime that performs the coding-agent work.
This module owns the boundary between them: deterministic executable discovery,
version health, Veya-owned runtime data/configuration, and the child-process
environment.  It deliberately does not implement a second coding executor.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

REASONIX_PACKAGE = "reasonix"
REASONIX_VERSION = "1.21.3"
REASONIX_COMMIT = "6cc0d73405c3ad12c38753f117bdfa01417ab898"
REASONIX_TARBALL_INTEGRITY = "sha512-F6aZEYvH+0FT9wmuBdC6F83S2xFY2KN6uKDoyb0mKrPtKQ8kid/VDXVQizGm7RGxdHjXI4NXbdhl+ybImhX1XQ=="

_VERSION_RE = re.compile(r"(?:^|\s)reasonix\s+v?([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)", re.I)
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class HicodeRuntimeError(RuntimeError):
    """The managed runtime is absent, unhealthy, or incorrectly configured."""


@dataclass(frozen=True)
class HicodeRuntimeStatus:
    """Secret-free readiness snapshot for diagnostics and release evidence."""

    managed_reasonix_available: bool
    managed_reasonix_version: str | None
    managed_reasonix_compatible: bool
    executable: str | None
    source: str | None
    config_path: str
    data_root: str
    error: str | None = None

    @property
    def healthy(self) -> bool:
        return bool(self.executable and self.managed_reasonix_compatible)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _version_from_output(output: str) -> str | None:
    match = _VERSION_RE.search(output.strip())
    if match:
        return match.group(1)
    return None


def _safe_url(value: str, *, fallback: str) -> str:
    candidate = value.strip() or fallback
    parsed = urlsplit(candidate)
    if parsed.username or parsed.password:
        raise HicodeRuntimeError("Hicode provider URL must not contain inline credentials")
    return candidate


def _safe_env_name(value: str | None) -> str | None:
    name = (value or "").strip()
    return name if _ENV_NAME_RE.fullmatch(name) else None


class HicodeExecutorAdapter:
    """Single Veya adapter for all Reasonix process/runtime boundary concerns."""

    def __init__(
        self,
        *,
        managed_root: str | Path | None = None,
        data_root: str | Path | None = None,
    ) -> None:
        self._managed_root_override = Path(managed_root).expanduser() if managed_root else None
        self._data_root_override = Path(data_root).expanduser() if data_root else None

    @property
    def managed_root(self) -> Path:
        configured = os.environ.get("HICODE_MANAGED_RUNTIME_ROOT", "").strip()
        return (
            Path(configured).expanduser() if configured else self._managed_root_override
        ) or Path("/opt/veya/hicode-runtime")

    @property
    def data_root(self) -> Path:
        configured = os.environ.get("HICODE_RUNTIME_DATA_ROOT", "").strip()
        if configured:
            return Path(configured).expanduser().resolve()
        if self._data_root_override is not None:
            return self._data_root_override.resolve()
        return (Path.home() / ".veya" / "hicode-runtime").resolve()

    @property
    def reasonix_home(self) -> Path:
        return self.data_root / "reasonix-home"

    @property
    def config_path(self) -> Path:
        return self.reasonix_home / ".reasonix" / "config.toml"

    @property
    def state_root(self) -> Path:
        return self.data_root / "state"

    def _managed_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        configured = os.environ.get("HICODE_MANAGED_BIN", "").strip()
        if configured:
            candidates.append(Path(configured).expanduser())
        root = self.managed_root
        candidates.extend(
            [
                root / "node_modules" / ".bin" / "reasonix",
                root / "bin" / "reasonix",
                root / "reasonix",
            ]
        )
        return candidates

    def _locate(self) -> tuple[Path, str]:
        """Resolve only Veya-managed paths, then the explicit dev override."""
        for candidate in self._managed_candidates():
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve(), "managed"

        explicit = os.environ.get("HICODE_BIN", "").strip()
        if explicit:
            if _truthy(os.environ.get("HICODE_PRODUCTION")):
                raise HicodeRuntimeError(
                    "HICODE_BIN override is disabled in production; install the Veya-managed runtime"
                )
            candidate = Path(explicit).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve(), "explicit_override"
            raise HicodeRuntimeError("HICODE_BIN points to a missing or non-executable file")

        raise HicodeRuntimeError(
            f"managed Reasonix {REASONIX_VERSION} is not installed; use the official Veya server image"
        )

    def resolve_binary(self) -> str:
        return str(self._locate()[0])

    def _probe_version(self, executable: Path) -> str | None:
        try:
            result = subprocess.run(
                [str(executable), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return _version_from_output(f"{result.stdout}\n{result.stderr}")

    def probe_version(self, executable: str | None = None) -> str | None:
        path = Path(executable) if executable else Path(self.resolve_binary())
        return self._probe_version(path)

    def status(self) -> HicodeRuntimeStatus:
        try:
            executable, source = self._locate()
        except HicodeRuntimeError as exc:
            return HicodeRuntimeStatus(
                managed_reasonix_available=False,
                managed_reasonix_version=None,
                managed_reasonix_compatible=False,
                executable=None,
                source=None,
                config_path=str(self.config_path),
                data_root=str(self.data_root),
                error=str(exc),
            )

        version = self._probe_version(executable)
        compatible = version == REASONIX_VERSION
        error = (
            None
            if compatible
            else f"expected Reasonix {REASONIX_VERSION}, detected {version or 'unknown'}"
        )
        return HicodeRuntimeStatus(
            managed_reasonix_available=source == "managed",
            managed_reasonix_version=version,
            managed_reasonix_compatible=compatible,
            executable=str(executable),
            source=source,
            config_path=str(self.config_path),
            data_root=str(self.data_root),
            error=error,
        )

    def ensure_compatible(self, executable: str | None = None) -> str:
        path = Path(executable) if executable else Path(self.resolve_binary())
        version = self._probe_version(path)
        if version != REASONIX_VERSION:
            raise HicodeRuntimeError(
                f"Reasonix version is incompatible; expected {REASONIX_VERSION}, detected {version or 'unknown'}"
            )
        return str(path)

    def _render_config(self) -> str:
        default_primary_base = (
            "http://127.0.0.1:10103/v1"
            if _truthy(os.environ.get("HICODE_PROXY"))
            else "http://127.0.0.1:10100/v1"
        )
        primary_base = _safe_url(
            os.environ.get("HICODE_REASONIX_BASE_URL", ""),
            fallback=default_primary_base,
        )
        primary_model = os.environ.get("HICODE_REASONIX_MODEL", "gpt-5.6-luna").strip()
        primary_provider = os.environ.get("HICODE_REASONIX_PROVIDER", "luna").strip() or "luna"
        primary_key_env = _safe_env_name(os.environ.get("HICODE_REASONIX_API_KEY_ENV"))
        cloud_base = _safe_url(
            os.environ.get("HICODE_REASONIX_CLOUD_BASE_URL", ""),
            fallback="https://opencode.ai/zen/go/v1",
        )
        cloud_model = os.environ.get("HICODE_REASONIX_CLOUD_MODEL", "deepseek-v4-flash").strip()
        cloud_key_env = _safe_env_name(
            os.environ.get("HICODE_REASONIX_CLOUD_API_KEY_ENV", "OPENCODE_API_KEY")
        )

        lines = [
            "# Generated by Veya HicodeExecutorAdapter; do not edit as a source artifact.",
            "[agent]",
            'reasoning_language = "auto"',
            "",
            "[[providers]]",
            f"name = {json.dumps(primary_provider)}",
            'kind = "openai"',
            f"base_url = {json.dumps(primary_base)}",
            f"model = {json.dumps(primary_model)}",
        ]
        if primary_key_env:
            lines.append(f"api_key_env = {json.dumps(primary_key_env)}")
        lines.extend(
            [
                "context_window = 1000000",
                "",
                "[[providers]]",
                'name = "opencode-go"',
                'kind = "openai"',
                f"base_url = {json.dumps(cloud_base)}",
                f"model = {json.dumps(cloud_model)}",
            ]
        )
        if cloud_key_env:
            lines.append(f"api_key_env = {json.dumps(cloud_key_env)}")
        lines.extend(["context_window = 1000000", "", "[environment]", "enabled = true", ""])
        return "\n".join(lines)

    def ensure_config(self) -> Path:
        path = self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._render_config(), encoding="utf-8")
        with suppress(OSError):
            path.chmod(0o600)
        self.state_root.mkdir(parents=True, exist_ok=True)
        return path

    def execution_environment(self) -> dict[str, str]:
        """Return a child environment with isolated, secret-free config paths.

        Credentials remain late-bound in the inherited process environment via
        ``api_key_env``.  The generated TOML contains variable names only.
        """
        self.ensure_config()
        env = os.environ.copy()
        env["HOME"] = str(self.reasonix_home)
        env["REASONIX_STATE_HOME"] = str(self.state_root)
        # Reasonix already supplies the outer coding sandbox.  Mark that
        # boundary so commands launched by the coding harness do not attempt a
        # second bubblewrap namespace inside it; the command runner still
        # enforces its policy and inherits the parent's filesystem/network
        # isolation.
        env["VEYA_SANDBOX_DEPTH"] = "1"
        return env

    def run_command(
        self,
        args: list[str],
        *,
        model: str,
        timeout: int,
        executable: str | None = None,
    ) -> list[str]:
        """Build the canonical CLI command; execution/cancellation stays injectable."""
        del timeout  # The async caller owns the wall-clock timeout around this command.
        return [
            self.ensure_compatible(executable),
            "run",
            *args,
            "--output-format",
            "stream-json",
            "--model",
            model,
            "--auto",
        ]

    def review_command(
        self,
        args: list[str],
        *,
        model: str,
        executable: str | None = None,
    ) -> list[str]:
        """Build the review command through the same runtime boundary."""
        return [self.ensure_compatible(executable), "review", "--model", model, *args]


_ADAPTER = HicodeExecutorAdapter()


def get_hicode_executor() -> HicodeExecutorAdapter:
    return _ADAPTER


def main() -> int:
    """Prepare the isolated config for the container entrypoint."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m server.hicode_runtime")
    parser.add_argument("command", choices=["prepare"], nargs="?", default="prepare")
    args = parser.parse_args()
    if args.command == "prepare":
        get_hicode_executor().ensure_config()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by image startup
    raise SystemExit(main())
