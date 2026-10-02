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

import hashlib
import json
import os
import re
import subprocess
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from server.hicode_cooldown import hicode_model_mapping
from veya.obase import canonical_proxies as _cp  # SPEC 10: no raw upstream ids in business code
from veya.remote.executor_registry import get_executor_registry

REASONIX_PACKAGE = "reasonix"
REASONIX_VERSION = "1.21.3"
REASONIX_COMMIT = "6cc0d73405c3ad12c38753f117bdfa01417ab898"
REASONIX_TARBALL_INTEGRITY = "sha512-F6aZEYvH+0FT9wmuBdC6F83S2xFY2KN6uKDoyb0mKrPtKQ8kid/VDXVQizGm7RGxdHjXI4NXbdhl+ybImhX1XQ=="

_VERSION_RE = re.compile(r"(?:^|\s)reasonix\s+v?([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)", re.I)
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_RUNTIME_MANIFEST = "runtime-manifest.json"
_RUNTIME_IMPORT_PACKAGES = ("obase", "oprim", "omodul", "oskill", "oservi", "docker")


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
    managed_python: str | None = None
    runtime_fingerprint: str | None = None
    runtime_preflight: bool = False

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
        if configured:
            return Path(configured).expanduser()
        if self._managed_root_override is not None:
            return self._managed_root_override
        user_managed = Path.home() / ".veya" / "hicode-managed-runtime"
        if user_managed.is_dir():
            return user_managed
        return Path("/opt/veya/hicode-runtime")

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

    @property
    def runtime_manifest_path(self) -> Path:
        return self.managed_root / _RUNTIME_MANIFEST

    def _path_within_managed_root(self, value: str | Path, *, label: str) -> Path:
        root = self.managed_root.resolve()
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if candidate != root and root not in candidate.parents:
            raise HicodeRuntimeError(f"{label} escapes the managed runtime root")
        return candidate

    def _lexical_path_within_managed_root(self, value: str | Path, *, label: str) -> Path:
        """Validate a managed path without resolving a venv's interpreter symlink."""
        root = self.managed_root.resolve()
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = Path(os.path.abspath(candidate))
        if candidate != root and root not in candidate.parents:
            raise HicodeRuntimeError(f"{label} escapes the managed runtime root")
        return candidate

    def _load_runtime_manifest(self) -> dict[str, Any]:
        path = self.runtime_manifest_path
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HicodeRuntimeError(f"managed runtime manifest is unreadable: {path}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise HicodeRuntimeError(f"managed runtime manifest is invalid: {path}")
        python_info = value.get("python")
        reasonix_info = value.get("reasonix")
        packages = value.get("packages")
        if (
            not isinstance(python_info, dict)
            or not all(python_info.get(key) for key in ("version", "executable", "site_packages"))
            or not isinstance(reasonix_info, dict)
            or not all(reasonix_info.get(key) for key in ("version", "binary", "commit"))
            or not isinstance(packages, dict)
            or any(
                not isinstance(packages.get(name), dict)
                or not all(packages[name].get(key) for key in ("version", "path"))
                for name in _RUNTIME_IMPORT_PACKAGES
            )
            or not isinstance(value.get("runtime_fingerprint"), str)
            or not value.get("runtime_fingerprint")
        ):
            raise HicodeRuntimeError(f"managed runtime manifest is incomplete: {path}")
        return value

    def _manifest_python_path(self, manifest: dict[str, Any]) -> Path:
        python_info = manifest.get("python")
        if not isinstance(python_info, dict) or not python_info.get("executable"):
            raise HicodeRuntimeError("managed runtime manifest has no Python executable")
        canonical = self._lexical_path_within_managed_root(
            str(python_info["executable"]), label="managed Python"
        )
        configured = os.environ.get("HICODE_MANAGED_PYTHON", "").strip()
        if configured:
            selected = self._lexical_path_within_managed_root(
                configured, label="HICODE_MANAGED_PYTHON"
            )
            if selected != canonical:
                raise HicodeRuntimeError(
                    "HICODE_MANAGED_PYTHON does not match the canonical managed manifest"
                )
        return canonical

    def _manifest_site_packages_path(self, manifest: dict[str, Any]) -> Path:
        python_info = manifest.get("python")
        if not isinstance(python_info, dict) or not python_info.get("site_packages"):
            raise HicodeRuntimeError("managed runtime manifest has no Python site-packages path")
        return self._path_within_managed_root(
            str(python_info["site_packages"]), label="managed site-packages"
        )

    def _runtime_fingerprint_payload(
        self,
        manifest: dict[str, Any],
        *,
        python_path: Path,
        reasonix_path: Path,
    ) -> dict[str, Any]:
        python_info = manifest["python"]
        reasonix_info = manifest["reasonix"]
        packages = manifest.get("packages")
        build_source = manifest.get("build_source")
        if not isinstance(packages, dict):
            raise HicodeRuntimeError("managed runtime manifest has no package set")
        if not isinstance(build_source, dict):
            raise HicodeRuntimeError("managed runtime manifest has no build-source evidence")
        package_payload: dict[str, Any] = {}
        for name in sorted(packages):
            package = packages[name]
            if not isinstance(package, dict):
                raise HicodeRuntimeError(f"managed runtime package metadata is invalid: {name}")
            package_payload[name] = {
                "version": package.get("version"),
                "path": str(self._path_within_managed_root(package["path"], label=name)),
                "source_sha": package.get("source_sha"),
                "metadata_sha256": package.get("metadata_sha256"),
            }
        return {
            "runtime_root": str(self.managed_root.resolve()),
            "python": {
                "executable": str(python_path),
                "version": python_info.get("version"),
                "site_packages": str(self._manifest_site_packages_path(manifest)),
                "executable_valid": python_info.get("executable_valid"),
                "stdlib_present": python_info.get("stdlib_present"),
                "encodings_import": python_info.get("encodings_import"),
                "pythonhome_override": python_info.get("pythonhome_override"),
                "sys_prefix": python_info.get("sys_prefix"),
                "sys_base_prefix": python_info.get("sys_base_prefix"),
                "stdlib": python_info.get("stdlib"),
                "encodings": python_info.get("encodings"),
            },
            "reasonix": {
                "binary": str(reasonix_path.resolve()),
                "version": reasonix_info.get("version"),
                "commit": reasonix_info.get("commit"),
            },
            "packages": package_payload,
            "build_source": build_source,
        }

    def _runtime_fingerprint(
        self,
        manifest: dict[str, Any],
        *,
        python_path: Path,
        reasonix_path: Path,
    ) -> str:
        payload = self._runtime_fingerprint_payload(
            manifest, python_path=python_path, reasonix_path=reasonix_path
        )
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def ensure_runtime_preflight(self) -> dict[str, Any]:
        """Fail closed before a model request if the managed Python ABI drifts."""
        manifest = self._load_runtime_manifest()
        python_path = self._manifest_python_path(manifest)
        site_packages = self._manifest_site_packages_path(manifest)
        if not python_path.is_file() or not os.access(python_path, os.X_OK):
            raise HicodeRuntimeError(
                f"HICODE_RUNTIME_INCOMPATIBLE: missing managed Python {python_path}"
            )
        if not site_packages.is_dir():
            raise HicodeRuntimeError(
                f"HICODE_RUNTIME_INCOMPATIBLE: missing managed site-packages {site_packages}"
            )

        try:
            reasonix_path, source = self._locate()
        except HicodeRuntimeError as exc:
            raise HicodeRuntimeError(f"HICODE_RUNTIME_INCOMPATIBLE: {exc}") from exc
        expected_reasonix = self._path_within_managed_root(
            str(manifest["reasonix"]["binary"]), label="managed Reasonix"
        )
        if source != "managed" or reasonix_path != expected_reasonix:
            raise HicodeRuntimeError(
                "HICODE_RUNTIME_INCOMPATIBLE: Reasonix is not the canonical managed binary"
            )
        reasonix_version = self._probe_version(reasonix_path)
        if reasonix_version != REASONIX_VERSION:
            raise HicodeRuntimeError(
                "HICODE_RUNTIME_INCOMPATIBLE: "
                f"expected Reasonix {REASONIX_VERSION}, detected {reasonix_version or 'unknown'}"
            )
        if manifest.get("reasonix", {}).get("version") != REASONIX_VERSION:
            raise HicodeRuntimeError("HICODE_RUNTIME_INCOMPATIBLE: Reasonix manifest version drift")

        probe = """
import importlib
import importlib.metadata as metadata
import hashlib
import json
import os
import sys
import sysconfig
from pathlib import Path

expected_python = Path(os.environ["HICODE_EXPECTED_PYTHON"]).absolute()
expected_python_real = expected_python.resolve()
actual_python = Path(sys.executable).resolve()
stdlib = Path(sysconfig.get_path("stdlib")).resolve()
encodings_file = stdlib / "encodings/__init__.py"
import encodings
if actual_python != expected_python_real:
    raise RuntimeError(f"managed Python executable mismatch: {actual_python} != {expected_python_real}")
if Path(sys.prefix).resolve() != expected_python.parent.parent.resolve():
    raise RuntimeError("managed Python sys.prefix does not match the venv root")
if not stdlib.is_dir() or not encodings_file.is_file():
    raise RuntimeError(f"managed Python stdlib/encodings missing: {encodings_file}")
if Path(encodings.__file__).resolve() != encodings_file:
    raise RuntimeError("encodings import did not resolve to the selected stdlib")
if os.environ.get("PYTHONHOME"):
    raise RuntimeError("PYTHONHOME override is not allowed")

names = ("obase", "oprim", "omodul", "oskill", "oservi", "docker")
packages = {}
for name in names:
    module = importlib.import_module(name)
    distribution = metadata.distribution(name)
    metadata_files = [
        path for path in (distribution.files or ()) if str(path).endswith(".dist-info/METADATA")
    ]
    if len(metadata_files) != 1:
        raise RuntimeError(f"metadata evidence is ambiguous for {name}")
    metadata_path = Path(distribution.locate_file(metadata_files[0]))
    packages[name] = {
        "version": metadata.version(name),
        "path": str(Path(module.__file__).resolve().parent),
        "metadata_sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
    }
from oprim._task_tier import TIER_FLAGSHIP
if type(TIER_FLAGSHIP) is not str or TIER_FLAGSHIP != "FLAGSHIP":
    raise RuntimeError("TIER_FLAGSHIP has incompatible type or value")
model_router = importlib.import_module("omodul.model_router")
print(json.dumps({
    "python_version": ".".join(str(part) for part in sys.version_info[:3]),
    "python_executable_valid": True,
    "stdlib_present": True,
    "encodings_import": True,
    "pythonhome_override": False,
    "sys_prefix": str(Path(sys.prefix).resolve()),
    "sys_base_prefix": str(Path(sys.base_prefix).resolve()),
    "stdlib": str(stdlib),
    "encodings": str(encodings_file),
    "packages": packages,
    "tier_flagship": TIER_FLAGSHIP,
    "hicode_bootstrap_path": str(Path(model_router.__file__).resolve()),
}, sort_keys=True))
"""
        env = os.environ.copy()
        for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP"):
            env.pop(key, None)
        env.update(
            {
                "HICODE_MANAGED_PYTHON": str(python_path),
                "HICODE_EXPECTED_PYTHON": str(python_path),
                "PYTHONNOUSERSITE": "1",
                "PYTHONSAFEPATH": "1",
                "PYTHONPATH": str(site_packages),
            }
        )
        result = subprocess.run(
            [str(python_path), "-P", "-c", probe],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-500:]
            raise HicodeRuntimeError(
                f"HICODE_RUNTIME_INCOMPATIBLE: managed 3O import surface failed: {detail}"
            )
        try:
            probe_result = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise HicodeRuntimeError(
                "HICODE_RUNTIME_INCOMPATIBLE: managed preflight returned invalid evidence"
            ) from exc
        if not isinstance(probe_result, dict):
            raise HicodeRuntimeError(
                "HICODE_RUNTIME_INCOMPATIBLE: managed preflight returned invalid evidence"
            )
        for key in ("python_executable_valid", "stdlib_present", "encodings_import"):
            if probe_result.get(key) is not True:
                raise HicodeRuntimeError(f"HICODE_RUNTIME_INCOMPATIBLE: {key} failed")
        if probe_result.get("pythonhome_override") is not False:
            raise HicodeRuntimeError("HICODE_RUNTIME_INCOMPATIBLE: PYTHONHOME override detected")

        expected_packages = manifest.get("packages")
        if not isinstance(expected_packages, dict):
            raise HicodeRuntimeError("HICODE_RUNTIME_INCOMPATIBLE: package manifest is missing")
        for name in _RUNTIME_IMPORT_PACKAGES:
            actual = probe_result.get("packages", {}).get(name, {})
            expected = expected_packages.get(name)
            if not isinstance(expected, dict) or not isinstance(actual, dict):
                raise HicodeRuntimeError(
                    f"HICODE_RUNTIME_INCOMPATIBLE: package evidence missing: {name}"
                )
            actual_path = self._path_within_managed_root(actual.get("path", ""), label=name)
            expected_path = self._path_within_managed_root(expected.get("path", ""), label=name)
            if (
                actual_path != expected_path
                or actual.get("version") != expected.get("version")
                or actual.get("metadata_sha256") != expected.get("metadata_sha256")
            ):
                raise HicodeRuntimeError(f"HICODE_RUNTIME_INCOMPATIBLE: package drift: {name}")
        if probe_result.get("python_version") != manifest["python"].get("version"):
            raise HicodeRuntimeError("HICODE_RUNTIME_INCOMPATIBLE: Python version drift")
        bootstrap_path = self._path_within_managed_root(
            probe_result.get("hicode_bootstrap_path", ""), label="Hicode bootstrap"
        )
        if (
            bootstrap_path
            != self._path_within_managed_root(
                str(expected_packages["omodul"]["path"]), label="omodul"
            )
            / "model_router.py"
        ):
            raise HicodeRuntimeError("HICODE_RUNTIME_INCOMPATIBLE: Hicode bootstrap path drift")

        fingerprint = self._runtime_fingerprint(
            manifest, python_path=python_path, reasonix_path=reasonix_path
        )
        if manifest.get("runtime_fingerprint") != fingerprint:
            raise HicodeRuntimeError(
                "HICODE_RUNTIME_INCOMPATIBLE: runtime fingerprint does not match manifest"
            )
        manifest_hash = hashlib.sha256(self.runtime_manifest_path.read_bytes()).hexdigest()
        record = {
            "hicode_runtime_root": str(self.managed_root.resolve()),
            "hicode_python": str(python_path),
            "hicode_python_version": probe_result["python_version"],
            "hicode_reasonix_version": reasonix_version,
            "hicode_python_executable_valid": probe_result["python_executable_valid"],
            "hicode_stdlib_present": probe_result["stdlib_present"],
            "hicode_encodings_import": probe_result["encodings_import"],
            "pythonhome_override": probe_result["pythonhome_override"],
            "hicode_python_sys_prefix": probe_result["sys_prefix"],
            "hicode_python_sys_base_prefix": probe_result["sys_base_prefix"],
            "hicode_python_stdlib": probe_result["stdlib"],
            "hicode_python_encodings": probe_result["encodings"],
            "hicode_oprim_version": expected_packages["oprim"]["version"],
            "hicode_oprim_path": str(
                self._path_within_managed_root(expected_packages["oprim"]["path"], label="oprim")
            ),
            "runtime_manifest_hash": manifest_hash,
            "runtime_fingerprint": fingerprint,
        }
        self.state_root.mkdir(parents=True, exist_ok=True)
        (self.state_root / "hicode-runtime-fingerprint.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return {
            **record,
            "packages": probe_result["packages"],
            "hicode_bootstrap_import": True,
            "owner_pythonpath_authority": False,
        }

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
        identity = get_executor_registry().identity("hicode")
        requested_model = (identity.model or "").strip()
        if not requested_model:
            raise HicodeRuntimeError("Hicode runtime identity has no configured model")
        try:
            mapping = hicode_model_mapping(requested_model)
            primary_model = str(mapping["requested_model"])
        except RuntimeError:
            # Test/dev providers may use an explicitly injected model. The
            # production Hicode identity is validated by the authority above.
            primary_model = requested_model
        primary_provider = (identity.provider or "").strip()
        if not primary_provider:
            raise HicodeRuntimeError("Hicode runtime identity has no configured provider")
        primary_key_env = _safe_env_name(os.environ.get("HICODE_REASONIX_API_KEY_ENV"))
        primary_api_key = os.environ.get(primary_key_env, "") if primary_key_env else ""
        cloud_base = _safe_url(
            os.environ.get("HICODE_REASONIX_CLOUD_BASE_URL", ""),
            fallback="https://opencode.ai/zen/go/v1",
        )
        cloud_model = os.environ.get(
            "HICODE_REASONIX_CLOUD_MODEL", ""
        ).strip() or _cp.executor_model("hicode_reasonix_cloud")
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
        if primary_api_key:
            lines.append(f'headers = {{ "x-api-key" = {json.dumps(primary_api_key)} }}')
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
        for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP"):
            env.pop(key, None)
        try:
            manifest = self._load_runtime_manifest()
            managed_python = self._manifest_python_path(manifest)
            site_packages = self._manifest_site_packages_path(manifest)
        except HicodeRuntimeError:
            managed_python = self.managed_root / "python" / "bin" / "python"
            site_packages = None
        if managed_python.is_file():
            env["HICODE_MANAGED_PYTHON"] = str(managed_python)
            env["HICODE_MANAGED_PYTHON_ROOT"] = str(managed_python.parent.parent)
            env["HICODE_MANAGED_PYTHON_AUTHORITY"] = "YES"
            env["HICODE_OWNER_PYTHONPATH_INHERITED"] = "NO"
            env["PYTHONNOUSERSITE"] = "1"
            env["PYTHONSAFEPATH"] = "1"
            env["PATH"] = os.pathsep.join([str(managed_python.parent), env.get("PATH", os.defpath)])
            if site_packages is not None and site_packages.is_dir():
                env["PYTHONPATH"] = str(site_packages)
        env["HICODE_MANAGED_RUNTIME_ROOT"] = str(self.managed_root.resolve())
        if managed_python.is_file():
            env["VIRTUAL_ENV"] = str(managed_python.parent.parent)
        with suppress(HicodeRuntimeError):
            manifest = self._load_runtime_manifest()
            fingerprint = manifest.get("runtime_fingerprint")
            if isinstance(fingerprint, str) and fingerprint:
                env["HICODE_RUNTIME_FINGERPRINT"] = fingerprint
        # Reasonix already supplies the outer coding sandbox.  Mark that
        # boundary so commands launched by the coding harness do not attempt a
        # second bubblewrap namespace inside it; the command runner still
        # enforces its policy and inherits the parent's filesystem/network
        # isolation.
        env["VEYA_SANDBOX_DEPTH"] = "1"
        return env

    def run_managed_bootstrap(
        self,
        *,
        request: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Run the canonical 3O import probe in managed Python.

        The host sends JSON only.  No Python objects, owner checkout paths, or
        credentials are placed in the payload; credentials remain late-bound in
        the child environment through the existing runtime configuration.
        """

        fingerprint = self.ensure_runtime_preflight()
        manifest = self._load_runtime_manifest()
        python_path = self._manifest_python_path(manifest)
        site_packages = self._manifest_site_packages_path(manifest)
        source_root = Path(__file__).resolve().parents[1]
        payload = {
            "execution_id": str(request.get("execution_id") or "")[:200],
            "workspace": str(request.get("workspace") or "")[:1000],
            "objective": str(request.get("objective") or "")[:2000],
            "model": str(request.get("model") or "")[:200],
            "provider": str(request.get("provider") or "")[:200],
            "context": dict(request.get("context") or {})
            if isinstance(request.get("context"), dict)
            else {},
            "managed_runtime_root": str(self.managed_root.resolve()),
            "runtime_fingerprint": fingerprint["runtime_fingerprint"],
        }
        env = self.execution_environment()
        env["PYTHONPATH"] = os.pathsep.join([str(source_root), str(site_packages)])
        env["HICODE_OWNER_PYTHONPATH_INHERITED"] = "NO"
        try:
            result = subprocess.run(
                [str(python_path), "-P", "-m", "server.hicode_managed_entry"],
                cwd=str(source_root),
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HicodeRuntimeError(
                f"HICODE_RUNTIME_INCOMPATIBLE: managed bootstrap failed: {exc}"
            ) from exc
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        try:
            evidence = json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            detail = (result.stderr or result.stdout).strip()[-2000:]
            raise HicodeRuntimeError(
                f"HICODE_RUNTIME_INCOMPATIBLE: managed bootstrap returned invalid evidence: {detail}"
            ) from exc
        if (
            result.returncode != 0
            or not isinstance(evidence, dict)
            or evidence.get("status") != "READY"
            or evidence.get("owner_3o_import_detected") is not False
            or evidence.get("owner_platform_3o_in_sys_path") is not False
        ):
            blocked_detail: Any = (
                evidence.get("failure_detail") if isinstance(evidence, dict) else evidence
            )
            raise HicodeRuntimeError(
                "HICODE_RUNTIME_INCOMPATIBLE: managed bootstrap blocked: "
                f"{str(blocked_detail)[:2000]}"
            )
        if evidence.get("runtime_fingerprint") != fingerprint["runtime_fingerprint"]:
            raise HicodeRuntimeError(
                "HICODE_RUNTIME_INCOMPATIBLE: managed bootstrap fingerprint drift"
            )
        return evidence

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
    parser.add_argument("command", choices=["prepare", "preflight"], nargs="?", default="prepare")
    args = parser.parse_args()
    if args.command == "prepare":
        get_hicode_executor().ensure_config()
    elif args.command == "preflight":
        get_hicode_executor().ensure_runtime_preflight()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by image startup
    raise SystemExit(main())
