"""Workspace isolation for the remote MCP gateway (spec §3).

The policy is deliberately conservative and independent of the caller:

* every path is canonicalized with ``Path.resolve()`` so ``..`` traversal and
  symlinks are resolved *before* containment is checked — a symlink that points
  outside the bound workspace is therefore rejected exactly like ``../``;
* a workspace root must be an explicitly bound directory; shallow system roots
  (``/``, ``/home``, ``/data``, ``/etc`` ...) can never be bound;
* sensitive subtrees (credentials, secret stores, kernel/device trees) are
  rejected even inside an otherwise valid workspace;
* writes to ``.git`` internals are refused unless the session holds the
  destructive capability (git itself remains the only writer of its metadata).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .models import EffectClass, RemotePermissions


class WorkspacePolicyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _home() -> Path:
    return Path.home().resolve()


def forbidden_workspace_roots() -> frozenset[Path]:
    """Exact roots that can never be bound as a remote workspace."""

    home = _home()
    raw = [
        "/",
        "/home",
        "/data",
        "/root",
        "/etc",
        "/usr",
        "/var",
        "/tmp",
        "/opt",
        "/srv",
        "/boot",
        "/proc",
        "/sys",
        "/dev",
        "/run",
        str(home),
    ]
    roots: set[Path] = set()
    for item in raw:
        try:
            roots.add(Path(item).resolve())
        except OSError:  # pragma: no cover - defensive
            continue
    return frozenset(roots)


def sensitive_subpaths() -> tuple[Path, ...]:
    """Credential / secret / kernel paths that are never accessible remotely."""

    home = _home()
    raw = [
        "/etc",
        "/proc",
        "/sys",
        "/dev",
        "/run/secrets",
        "/var/run/secrets",
        str(home / ".ssh"),
        str(home / ".config"),
        str(home / ".aws"),
        str(home / ".gnupg"),
        str(home / ".kube"),
        str(home / ".docker"),
        str(home / ".netrc"),
        str(home / ".git-credentials"),
    ]
    out: list[Path] = []
    for item in raw:
        try:
            out.append(Path(item).resolve())
        except OSError:  # pragma: no cover - defensive
            continue
    return tuple(out)


_DESTRUCTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("rm", re.compile(r"(^|[\s;&|])rm(\s|$)")),
    ("rmdir", re.compile(r"(^|[\s;&|])rmdir(\s|$)")),
    ("shred", re.compile(r"(^|[\s;&|])shred(\s|$)")),
    ("dd", re.compile(r"(^|[\s;&|])dd(\s|$)")),
    ("mkfs", re.compile(r"(^|[\s;&|])mkfs")),
    ("chmod", re.compile(r"(^|[\s;&|])chmod(\s|$)")),
    ("chown", re.compile(r"(^|[\s;&|])chown(\s|$)")),
    ("kill", re.compile(r"(^|[\s;&|])kill(all)?(\s|$)")),
    ("git-reset-hard", re.compile(r"git\s+reset\b[^\n]*--hard")),
    ("git-clean", re.compile(r"git\s+clean\b")),
    ("git-push-force", re.compile(r"git\s+push\b[^\n]*(--force|-f)(\s|$)")),
    ("git-checkout-discard", re.compile(r"git\s+checkout\b[^\n]*--\s")),
    ("systemctl", re.compile(r"(^|[\s;&|])systemctl(\s|$)")),
    ("service", re.compile(r"(^|[\s;&|])service(\s|$)")),
    ("sudo", re.compile(r"(^|[\s;&|])sudo(\s|$)")),
    ("apt", re.compile(r"(^|[\s;&|])(apt|apt-get|yum|dnf|apk|pacman)(\s|$)")),
    ("pip-install", re.compile(r"\bpip3?\s+install\b")),
    ("npm-install-global", re.compile(r"\bnpm\s+install\b[^\n]*\s-g(\s|$)")),
    ("curl-pipe-sh", re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(sh|bash)\b")),
    ("truncate", re.compile(r"(^|[\s;&|])truncate(\s|$)")),
    ("credential-write", re.compile(r"(?i)>\s*[^\s]*\.(ssh|aws|gnupg)")),
)


def classify_destructive(command: str) -> str | None:
    """Return the matched destructive pattern name, or None if the command is benign."""

    if not command or not command.strip():
        return None
    for name, pattern in _DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            return name
    return None


@dataclass
class WorkspacePolicy:
    """Path and permission policy scoped to one bound workspace.

    ``extra_roots`` mirrors the canonical tools' ``VEYA_WORKSPACE_EXTRA_DIRS``
    so the adapter's containment check matches what the ToolRuntime accepts;
    otherwise a path the runtime allows is rejected earlier with a misleading
    "escapes workspace root".
    """

    root: Path
    permissions: RemotePermissions
    extra_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        candidate = Path(self.root).expanduser()
        if not candidate.exists():
            raise WorkspacePolicyError("WORKSPACE_DENIED", f"workspace does not exist: {candidate}")
        resolved = candidate.resolve()
        if not resolved.is_dir():
            raise WorkspacePolicyError(
                "WORKSPACE_DENIED", f"workspace is not a directory: {resolved}"
            )
        if resolved in forbidden_workspace_roots():
            raise WorkspacePolicyError(
                "WORKSPACE_DENIED", f"refusing to bind system root as workspace: {resolved}"
            )
        self.root = resolved
        allowed_extras: list[Path] = []
        for item in self.extra_roots:
            try:
                extra = Path(item).expanduser().resolve()
            except OSError:  # pragma: no cover - defensive
                continue
            if extra.is_dir() and extra != resolved and extra not in allowed_extras:
                allowed_extras.append(extra)
        self.extra_roots = tuple(allowed_extras)

    @property
    def roots(self) -> tuple[Path, ...]:
        return (self.root, *self.extra_roots)

    # ── path resolution ─────────────────────────────────────────────
    def resolve(
        self, path: str | Path, *, must_exist: bool | None = True, for_write: bool = False
    ) -> Path:
        text = str(path or ".")
        if "\x00" in text:
            raise WorkspacePolicyError("WORKSPACE_DENIED", "path contains NUL byte")
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve(strict=False)
        if not any(resolved == allowed or allowed in resolved.parents for allowed in self.roots):
            raise WorkspacePolicyError("WORKSPACE_DENIED", f"path escapes workspace root: {text!r}")
        for sensitive in sensitive_subpaths():
            if resolved == sensitive or sensitive in resolved.parents:
                raise WorkspacePolicyError(
                    "WORKSPACE_DENIED", f"path is a protected location: {text!r}"
                )
        if for_write:
            git_dir = self.root / ".git"
            inside_git = resolved == git_dir or git_dir in resolved.parents
            if inside_git and not self.permissions.destructive:
                raise WorkspacePolicyError(
                    "POLICY_BLOCKED", "writing .git internals requires destructive capability"
                )
        if for_write and must_exist is False and resolved.exists() and resolved.is_dir():
            raise WorkspacePolicyError(
                "INVALID_ARGUMENT", f"refusing to overwrite directory: {text!r}"
            )
        if must_exist is True and not resolved.exists():
            raise WorkspacePolicyError("NOT_FOUND", f"path not found: {text!r}")
        if must_exist is False and resolved.exists():
            raise WorkspacePolicyError("POLICY_BLOCKED", f"path already exists: {text!r}")
        return resolved

    # ── permission checks ───────────────────────────────────────────
    def require(
        self,
        effect: EffectClass,
        *,
        needs_shell: bool = False,
        needs_git: bool = False,
    ) -> None:
        if effect is EffectClass.READ:
            if not self.permissions.read:
                raise WorkspacePolicyError("TOOL_DENIED", "session lacks read permission")
        elif effect is EffectClass.DESTRUCTIVE:
            if not self.permissions.destructive:
                raise WorkspacePolicyError(
                    "TOOL_DENIED", "destructive capability is not granted to this session"
                )
        elif not self.permissions.write:
            raise WorkspacePolicyError("TOOL_DENIED", "session lacks write permission")
        if needs_shell and not self.permissions.shell:
            raise WorkspacePolicyError("TOOL_DENIED", "session lacks shell permission")
        if needs_git and not self.permissions.git:
            raise WorkspacePolicyError("TOOL_DENIED", "session lacks git permission")

    def require_not_destructive(self, command: str) -> None:
        matched = classify_destructive(command)
        if matched is not None and not self.permissions.destructive:
            raise WorkspacePolicyError(
                "POLICY_BLOCKED",
                f"command classified destructive ({matched}); destructive capability not granted",
            )
