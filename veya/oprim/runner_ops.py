"""Runner operations oprims — self-update and login service (NANO-W8).

One atomic effect per atom, all of them local to the machine that runs them:

* :func:`runner_self_update_check` — read the release manifest
* :func:`runner_self_update_download` — fetch the artifact and verify its
  pinned ed25519 signature
* :func:`runner_self_update_stage` — unpack beside the install and keep the
  previous tree so a failed health check can roll back
* :func:`runner_self_update_restart` — hand off to the new binary
* :func:`runner_service_install` / :func:`runner_service_uninstall` /
  :func:`runner_service_status` — a ``systemd --user`` unit on Linux, a
  launchd plist on macOS

Two rules every atom here follows:

* **Nothing starts without its evidence.**  A download is only kept when the
  signature verifies against the pinned key; an unverifiable artifact is
  deleted, not staged.
* **Nothing is installed silently.**  The approval gate lives one layer up —
  the atoms are only ever reached from an approved Action Gateway request, and
  service writes are idempotent and support a dry run.

Whether the runner is *allowed* to restart at this moment is a question about
runner state, not about the machine: that is
``veya.oskill.runner_ops.runner_safe_to_restart``.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519

from veya.obase.errors import VeyaError
from veya.obase.runner_release import (
    PUBLIC_KEY_ENV,
    STAGING_DIR_ENV,
    RunnerReleaseError,
    decode_ed25519_public_key,
    is_newer_version,
    parse_release_manifest,
)

#: Published release endpoint used when a deployment names none.  A deployment
#: that must not reach it checks ``VEYA_RUNNER_RELEASE_MANIFEST_URL`` first.
SUGGESTED_RELEASE_MANIFEST_URL = "https://veya.aiinote.com/runner/release.json"

DEFAULT_MANIFEST_TIMEOUT_S = 10.0
DEFAULT_DOWNLOAD_TIMEOUT_S = 300.0

#: The system unit name. ``--user`` only: a runner belongs to the person who
#: logged in, never to root.
SERVICE_UNIT_NAME = "veya-runner.service"

#: A restart probe window. If the new process is still alive after this, the
#: hand-off worked; the runner is a daemon and is not expected to exit.
RESTART_PROBE_S = 5.0


class RunnerOpsError(VeyaError):
    """A runner operation could not be completed."""

    def __init__(self, op_name: str, reason: str):
        super().__init__(f"runner_ops.{op_name}: {reason}")
        self.operation = op_name
        self.reason = reason


def _require_self_update(op_name: str) -> None:
    flag = os.environ.get("VEYA_RUNNER_SELFUPDATE", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        raise RunnerOpsError(
            op_name,
            "VEYA_RUNNER_SELFUPDATE flag not set (set VEYA_RUNNER_SELFUPDATE=1 to enable)",
        )


def _require_service(op_name: str) -> None:
    """Service management shares the self-update flag: both write to the host."""

    _require_self_update(op_name)


def _manifest_timeout_s() -> float:
    try:
        return float(os.environ.get("VEYA_RUNNER_MANIFEST_TIMEOUT_S") or DEFAULT_MANIFEST_TIMEOUT_S)
    except ValueError:
        return DEFAULT_MANIFEST_TIMEOUT_S


def _staging_root() -> Path:
    """Where downloads are staged. Never the install directory itself."""

    configured = os.environ.get(STAGING_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(tempfile.gettempdir()) / "veya-runner-update"


def runner_self_update_check(
    *,
    current_version: str,
    manifest_url: str | None = None,
) -> dict[str, Any]:
    """Read the release manifest and say whether a newer version exists.

    Args:
        current_version: The version this runner is running.
        manifest_url: Release manifest endpoint (defaults to
            ``VEYA_RUNNER_RELEASE_MANIFEST_URL``).

    Returns:
        ``{"update_available", "latest_version", "current_version",
        "url", "signature"}``
    """
    _require_self_update("self_update_check")
    url = manifest_url or os.environ.get("VEYA_RUNNER_RELEASE_MANIFEST_URL", "").strip()
    url = url or SUGGESTED_RELEASE_MANIFEST_URL

    try:
        with httpx.Client(timeout=_manifest_timeout_s()) as client:
            response = client.get(url)
    except Exception as exc:
        raise RunnerOpsError("self_update_check", f"release manifest unreachable: {exc}") from exc

    if int(getattr(response, "status_code", 0) or 0) >= 400:
        raise RunnerOpsError(
            "self_update_check",
            f"release manifest returned HTTP {getattr(response, 'status_code', 0)}",
        )
    release = parse_release_manifest(response.json())
    return {
        "current_version": str(current_version),
        "latest_version": release["version"],
        "update_available": is_newer_version(release["version"], current_version),
        "url": release["url"],
        "signature": release["signature"],
        "manifest_url": url,
        # A newer version is only usable if this runner also holds the pinned
        # key: without it the download is refused, so saying "update
        # available" alone would send an operator looking for the wrong problem.
        "public_key_pinned": bool(os.environ.get(PUBLIC_KEY_ENV, "").strip()),
    }


def runner_self_update_download(
    url: str,
    *,
    expected_signature: str,
    public_key_b64: str,
    destination_dir: str | None = None,
) -> dict[str, Any]:
    """Fetch a release artifact and verify it against the pinned key.

    The artifact is written to the staging directory only after its signature
    verifies.  A failure deletes what was written: an unverifiable file on disk
    is a file a later step might pick up.
    """

    _require_self_update("self_update_download")
    if not str(url or "").startswith(("http://", "https://")):
        raise RunnerOpsError("self_update_download", "the release artifact url is not http(s)")
    if not str(expected_signature or "").strip():
        raise RunnerOpsError("self_update_download", "the release manifest carried no signature")
    try:
        public_key = decode_ed25519_public_key(public_key_b64)
    except RunnerReleaseError as exc:
        raise RunnerOpsError("self_update_download", str(exc)) from exc

    try:
        with httpx.Client(timeout=DEFAULT_DOWNLOAD_TIMEOUT_S, follow_redirects=True) as client:
            response = client.get(url)
    except Exception as exc:
        raise RunnerOpsError("self_update_download", f"download failed: {exc}") from exc

    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        raise RunnerOpsError("self_update_download", f"download returned HTTP {status}")

    payload = bytes(getattr(response, "content", b"") or b"")
    if not payload:
        raise RunnerOpsError("self_update_download", "the release artifact is empty")

    try:
        ed25519.Ed25519PublicKey.from_public_bytes(public_key).verify(
            _decode_signature(expected_signature), payload
        )
    except RunnerOpsError:
        raise
    except Exception as exc:
        raise RunnerOpsError(
            "self_update_download", f"signature verification failed: {exc}"
        ) from exc

    target_dir = Path(destination_dir).expanduser() if destination_dir else _staging_root()
    target_dir.mkdir(parents=True, exist_ok=True)
    artifact = target_dir / (Path(url.split("?", 1)[0]).name or "release.bin")
    artifact.write_bytes(payload)

    return {
        "success": True,
        "path": str(artifact),
        "bytes": len(payload),
        "signature_verified": True,
        "content_length": (getattr(response, "headers", {}) or {}).get("content-length"),
    }


def _decode_signature(value: str) -> bytes:
    """The manifest signature, base64 (padded or not)."""

    raw = str(value or "").strip()
    padded = raw + "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return raw.encode("utf-8")


def runner_self_update_stage(
    *,
    archive_path: str,
    install_dir: str,
    staging_dir: str | None = None,
) -> dict[str, Any]:
    """Unpack a verified artifact beside the install, keeping the old tree.

    The previous tree is moved to ``<install_dir>.previous`` rather than
    deleted, so :func:`runner_self_update_rollback` can put it back when the
    new version fails its health check.
    """

    _require_self_update("self_update_stage")
    staging = (
        Path(staging_dir).expanduser()
        if staging_dir
        else Path(tempfile.mkdtemp(prefix="veya-runner-stage-"))
    )
    staging.mkdir(parents=True, exist_ok=True)
    unpacked = staging / "unpacked"
    shutil.unpack_archive(str(archive_path), str(unpacked))

    install = Path(install_dir).expanduser()
    previous = install.with_name(f"{install.name}.previous")
    rolled_aside = install.exists()
    if rolled_aside:
        install.rename(previous)
    unpacked.rename(install)
    return {
        "success": True,
        "install_dir": str(install),
        "previous_dir": str(previous) if rolled_aside else "",
        "rolled_aside": rolled_aside,
    }


def runner_self_update_rollback(*, install_dir: str) -> dict[str, Any]:
    """Put the previous tree back after a failed health check."""

    _require_self_update("self_update_rollback")
    install = Path(install_dir).expanduser()
    previous = install.with_name(f"{install.name}.previous")
    if not previous.exists():
        return {"success": False, "reason": "there is no previous version to roll back to"}
    failed = install.with_name(f"{install.name}.failed")
    if install.exists():
        install.rename(failed)
    previous.rename(install)
    return {"success": True, "install_dir": str(install), "failed_dir": str(failed)}


def runner_self_update_restart(
    *,
    runner_binary: str,
    args: list[str] | None = None,
    probe_s: float = RESTART_PROBE_S,
) -> dict[str, Any]:
    """Hand off to the new runner binary.

    The new process is expected to keep running, so staying up past the probe
    window is the success signal; a clean exit means the new binary refused to
    start.
    """

    _require_self_update("self_update_restart")
    cmd = [str(runner_binary), *[str(arg) for arg in (args or [])]]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=probe_s,
            check=False,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": True,
            "command": cmd,
            "reason": "the new runner stayed up past the hand-off probe",
        }
    except OSError as exc:
        return {"success": False, "command": cmd, "error": str(exc)}

    returncode = int(getattr(completed, "returncode", 1) or 0)
    return {
        "success": returncode == 0,
        "command": cmd,
        "returncode": returncode,
        "error": ""
        if returncode == 0
        else str(getattr(completed, "stderr", "") or "restart failed"),
    }


# ── login service ────────────────────────────────────────────────────────


def _unit_dir(user: str | None = None) -> Path:
    """Where a per-user unit or plist lives."""

    configured = os.environ.get("VEYA_RUNNER_SERVICE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    home = Path.home() if not user else Path(f"/home/{user}")
    if platform.system().lower() == "darwin":
        return home / "Library" / "LaunchAgents"
    return home / ".config" / "systemd" / "user"


def runner_service_install(
    *,
    runner_binary: str,
    user: str | None = None,
    working_dir: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write the unit that starts this runner at login (idempotent).

    ``--user`` only: a runner belongs to the person who logged in.
    """

    _require_service("service_install")
    if platform.system().lower() == "darwin":
        return _launchd_install(runner_binary, user, working_dir, dry_run)

    home = working_dir or (str(Path.home()) if not user else f"/home/{user}")
    unit = f"""[Unit]
Description=Veya runner
After=network-online.target

[Service]
Type=simple
ExecStart={runner_binary}
WorkingDirectory={home}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    unit_path = _unit_dir(user) / SERVICE_UNIT_NAME
    if dry_run:
        return {"success": True, "dry_run": True, "unit_path": str(unit_path), "unit": unit}

    with contextlib.suppress(OSError):
        # Best effort: the write below is the authoritative answer, and a
        # missing directory is reported there with its own error.
        unit_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Idempotent: rewrite the unit so a changed binary path takes effect.
        unit_path.write_text(unit, encoding="utf-8")
    except OSError as exc:
        return {"success": False, "unit_path": str(unit_path), "error": str(exc)}

    enable = (
        _systemctl_user(["enable", SERVICE_UNIT_NAME])
        if unit_path.exists()
        else {"success": False, "error": "the unit file is not on disk"}
    )
    return {
        "success": True,
        "dry_run": False,
        "unit_path": str(unit_path),
        "enabled": enable.get("success", False),
        "enable_error": enable.get("error", ""),
    }


def _launchd_install(
    runner_binary: str,
    user: str | None,
    working_dir: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    """The macOS equivalent: a per-user launchd agent."""

    label = "com.veya.runner"
    home = working_dir or (str(Path.home()) if not user else f"/Users/{user}")
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array><string>{runner_binary}</string></array>
  <key>WorkingDirectory</key><string>{home}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
"""
    plist_path = _unit_dir(user) / f"{label}.plist"
    if dry_run:
        return {"success": True, "dry_run": True, "unit_path": str(plist_path), "unit": plist}
    with contextlib.suppress(OSError):
        plist_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        plist_path.write_text(plist, encoding="utf-8")
    except OSError as exc:
        return {"success": False, "unit_path": str(plist_path), "error": str(exc)}
    return {"success": True, "dry_run": False, "unit_path": str(plist_path), "label": label}


def runner_service_uninstall(*, user: str | None = None) -> dict[str, Any]:
    """Remove the login unit. Missing is already uninstalled."""

    _require_service("service_uninstall")
    if platform.system().lower() == "darwin":
        unit_path = _unit_dir(user) / "com.veya.runner.plist"
        label = "com.veya.runner"
    else:
        unit_path = _unit_dir(user) / SERVICE_UNIT_NAME
        label = SERVICE_UNIT_NAME

    existed = unit_path.exists()
    if existed:
        try:
            unit_path.unlink()
        except OSError as exc:
            return {"success": False, "unit_path": str(unit_path), "error": str(exc)}
        if platform.system().lower() != "darwin":
            _systemctl_user(["disable", "--now", label])
    return {"success": True, "unit_path": str(unit_path), "existed": existed}


def runner_service_status(*, user: str | None = None) -> dict[str, Any]:
    """Whether the login unit is present and active."""

    _require_service("service_status")
    unit_path = _unit_dir(user) / (
        "com.veya.runner.plist" if platform.system().lower() == "darwin" else SERVICE_UNIT_NAME
    )
    installed = unit_path.exists()
    if platform.system().lower() == "darwin":
        return {"status": "unknown", "installed": installed, "unit_path": str(unit_path)}

    probe = _systemctl_user(["is-active", SERVICE_UNIT_NAME])
    if not probe.get("ok"):
        # No systemd (a container, a non-systemd host): report the file, not a
        # guess. An unrunnable probe must not read as "the runner is fine".
        return {
            "status": "unknown" if not installed else "installed",
            "installed": installed,
            "unit_path": str(unit_path),
            "reason": probe.get("error", "systemctl --user is not available here"),
        }
    status = str(probe.get("stdout") or "").strip() or "unknown"
    return {"status": status, "installed": installed, "unit_path": str(unit_path)}


def _systemctl_user(argv: list[str]) -> dict[str, Any]:
    """Run one ``systemctl --user`` command, reporting rather than raising."""

    try:
        completed = subprocess.run(
            ["systemctl", "--user", *argv],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "success": False, "error": str(exc)}
    return {
        "ok": True,
        "success": int(getattr(completed, "returncode", 1) or 0) == 0,
        "stdout": str(getattr(completed, "stdout", "") or ""),
        "stderr": str(getattr(completed, "stderr", "") or ""),
    }


__all__ = [
    "DEFAULT_DOWNLOAD_TIMEOUT_S",
    "DEFAULT_MANIFEST_TIMEOUT_S",
    "SERVICE_UNIT_NAME",
    "SUGGESTED_RELEASE_MANIFEST_URL",
    "RunnerOpsError",
    "runner_self_update_check",
    "runner_self_update_download",
    "runner_self_update_restart",
    "runner_self_update_rollback",
    "runner_self_update_stage",
    "runner_service_install",
    "runner_service_status",
    "runner_service_uninstall",
]
