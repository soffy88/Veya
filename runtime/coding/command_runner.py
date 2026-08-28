"""Safe, evidence-producing command execution for coding worktrees."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from .models import CommandResult
from .sandbox_profiles import SandboxProfile, get_sandbox_profile


class CommandPolicyError(ValueError):
    """A command cannot be represented safely by the coding runner."""


_SHELLS = {"sh", "bash", "zsh", "fish", "dash", "cmd", "powershell", "pwsh"}
_SHELL_OPERATOR_TOKENS = {";", "&", "&&", "|", "||", "<", ">", "<<", ">>", "(", ")"}
_DESTRUCTIVE_EXECUTABLES = {
    "chmod",
    "chown",
    "dd",
    "mkfs",
    "mkfs.ext4",
    "rm",
    "rmdir",
    "shred",
    "unlink",
}
_NETWORK_EXECUTABLES = {"curl", "ftp", "nc", "netcat", "scp", "sftp", "ssh", "wget"}
_SECRET_NAME = re.compile(r"(?i)(api[_-]?key|auth(?:orization)?|password|passwd|secret|token)")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|authorization|password|passwd|secret|token)\b\s*[=:]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def parse_command(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, str):
        if not command.strip():
            raise CommandPolicyError("command must not be empty")
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
            lexer.whitespace_split = True
            lexer.commenters = ""
            argv = list(lexer)
        except ValueError as exc:
            raise CommandPolicyError(f"invalid command quoting: {exc}") from exc
        if any(token in _SHELL_OPERATOR_TOKENS for token in argv):
            raise CommandPolicyError("shell operators are not allowed; pass an argv command")
    else:
        argv = list(command)
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise CommandPolicyError("command argv must contain non-empty strings")
    executable = Path(argv[0]).name.lower()
    if executable in _SHELLS:
        raise CommandPolicyError("shell interpreters are not allowed by coding_run_command")
    return argv


def _git_operation(argv: list[str]) -> str | None:
    if Path(argv[0]).name != "git" or len(argv) < 2:
        return None
    for item in argv[1:]:
        if item.startswith("-"):
            continue
        return item.lower()
    return None


def command_requires_approval(argv: Sequence[str]) -> bool:
    """Return whether a command has an obvious destructive or remote effect."""
    if not argv:
        return True
    executable = Path(argv[0]).name.lower()
    args = [item.lower() for item in argv[1:]]
    if executable in _DESTRUCTIVE_EXECUTABLES:
        return True
    git_op = _git_operation(list(argv))
    if git_op in {"clean", "fetch", "gc", "pull", "push", "rebase", "reset", "restore", "clone"}:
        return True
    if git_op == "checkout" and any(item in {"-b", "--orphan"} for item in args):
        return True
    if git_op == "branch" and any(item in {"-d", "-D", "--delete", "--force"} for item in args):
        return True
    return executable in {"pip", "pip3", "npm", "pnpm", "yarn", "bun", "cargo", "go"} and any(
        item in {"install", "add", "remove", "uninstall", "update", "upgrade", "get"}
        for item in args
    )


def command_may_use_network(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    executable = Path(argv[0]).name.lower()
    if executable in _NETWORK_EXECUTABLES:
        return True
    git_op = _git_operation(list(argv))
    if git_op in {"clone", "fetch", "pull", "push", "submodule"}:
        return True
    if executable in {"pip", "pip3", "npm", "pnpm", "yarn", "bun", "cargo"}:
        args = {item.lower() for item in argv[1:]}
        return bool(args & {"install", "add", "remove", "uninstall", "update", "upgrade"})
    return False


def redact_text(value: str, *, secret_values: Sequence[str] = ()) -> str:
    redacted = value
    for secret in secret_values:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", redacted)
    return _BEARER.sub("Bearer [REDACTED]", redacted)


def _safe_environment(extra: Mapping[str, str] | None) -> dict[str, str]:
    """Keep useful process settings while excluding credential-shaped values."""
    allowed = {"LANG", "LC_ALL", "PATH", "PATHEXT", "SYSTEMROOT", "TMPDIR"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    for key, value in (extra or {}).items():
        if _SECRET_NAME.search(key):
            continue
        environment[str(key)] = str(value)
    return environment


def _within(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


class CommandRunner:
    """Run argv commands in a worktree and capture a redacted result artifact."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        profile: str | SandboxProfile = "local_restricted",
        artifact_root: str | Path | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        if not self.workspace_root.is_dir():
            raise CommandPolicyError(f"workspace root is not a directory: {self.workspace_root}")
        self.profile = get_sandbox_profile(profile)
        self.artifact_root = Path(artifact_root).expanduser().resolve() if artifact_root else None
        if self.artifact_root and not _within(self.workspace_root, self.artifact_root):
            raise CommandPolicyError("command artifacts must stay inside the workspace root")

    def _result(
        self,
        *,
        command: str,
        argv: list[str],
        cwd: Path,
        status: str,
        exit_code: int | None,
        stdout: str = "",
        stderr: str = "",
        duration_ms: float = 0.0,
        timed_out: bool = False,
        requires_approval: bool = False,
    ) -> CommandResult:
        secret_values = [
            value
            for key, value in os.environ.items()
            if _SECRET_NAME.search(key) and value
        ]
        result = CommandResult(
            command=redact_text(command, secret_values=secret_values),
            argv=[redact_text(item, secret_values=secret_values) for item in argv],
            cwd=str(cwd),
            profile=self.profile.id,
            status=status,  # type: ignore[arg-type]
            exit_code=exit_code,
            stdout=redact_text(stdout[:200_000], secret_values=secret_values),
            stderr=redact_text(stderr[:200_000], secret_values=secret_values),
            duration_ms=round(duration_ms, 3),
            timed_out=timed_out,
            requires_approval=requires_approval,
        )
        if self.artifact_root:
            self.artifact_root.mkdir(parents=True, exist_ok=True)
            artifact = self.artifact_root / f"command-result-{uuid.uuid4().hex[:12]}.json"
            result.artifact_path = str(artifact)
            artifact.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def _docker_argv(self, argv: list[str], cwd: Path, network: str | None) -> list[str]:
        if not self.profile.image:
            raise CommandPolicyError(f"sandbox profile has no Docker image: {self.profile.id}")
        network_mode = network or ("bridge" if self.profile.network == "allowed" else "none")
        if network_mode not in {"none", "bridge", "host"}:
            raise CommandPolicyError("Docker network must be none, bridge, or host")
        relative_cwd = cwd.relative_to(self.workspace_root)
        container_cwd = Path("/workspace") / relative_cwd
        wrapped = [
            "docker",
            "run",
            "--rm",
            "--network",
            network_mode,
            "--workdir",
            str(container_cwd),
            "--mount",
            f"type=bind,src={self.workspace_root},dst=/workspace,rw",
        ]
        # Deliberately no HOME, credential, or secret mounts.  The profile's
        # declarative mounts are currently limited to the task workspace.
        wrapped.extend([self.profile.image, *argv])
        return wrapped

    def _local_restricted_argv(self, argv: list[str], cwd: Path) -> list[str]:
        """Use bubblewrap when available so local restricted means real isolation."""
        bubblewrap = shutil.which("bwrap")
        if not bubblewrap:
            raise CommandPolicyError("local_restricted requires bubblewrap for filesystem/network isolation")
        wrapped = [
            bubblewrap,
            "--die-with-parent",
            "--ro-bind",
            "/",
            "/",
        ]
        # The host root is needed for normal interpreters and shared libraries,
        # but user homes and runtime sockets must not become readable defaults.
        # The task bind is appended afterwards so a workspace located below one
        # of these directories remains visible to the child.
        for private_path in ("/home", "/root", "/run"):
            if Path(private_path).is_dir():
                wrapped.extend(["--tmpfs", private_path])
        wrapped.extend([
            "--bind",
            str(self.workspace_root),
            str(self.workspace_root),
            "--chdir",
            str(cwd),
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--unshare-net",
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "PYTHONNOUSERSITE",
            "1",
        ])
        # A pytest temp workspace can itself live below /tmp.  In that case
        # the explicit workspace bind must remain visible; the read-only root
        # still prevents persistent writes elsewhere.  Production worktrees
        # normally get an ephemeral /tmp instead.
        if Path("/tmp") not in self.workspace_root.parents and self.workspace_root != Path("/tmp"):
            insert_at = wrapped.index("--bind")
            wrapped[insert_at:insert_at] = ["--tmpfs", "/tmp"]
        wrapped.extend(["--", *argv])
        return wrapped

    def run(
        self,
        command: str | Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout_s: float = 900,
        approved: bool = False,
        env: Mapping[str, str] | None = None,
        network: str | None = None,
    ) -> CommandResult:
        started = time.monotonic()
        argv = parse_command(command)
        command_text = command if isinstance(command, str) else " ".join(shlex.quote(item) for item in argv)
        target = (Path(cwd).expanduser() if cwd else self.workspace_root).resolve()
        if not target.is_dir() or not _within(self.workspace_root, target):
            return self._result(
                command=command_text,
                argv=argv,
                cwd=target,
                status="denied",
                exit_code=None,
                stderr="cwd must remain inside the task workspace",
                duration_ms=(time.monotonic() - started) * 1000,
            )
        if timeout_s <= 0:
            raise CommandPolicyError("timeout_s must be positive")
        if self.profile.network == "denied" and command_may_use_network(argv):
            return self._result(
                command=command_text,
                argv=argv,
                cwd=target,
                status="denied",
                exit_code=None,
                stderr="network access is denied by sandbox profile",
                duration_ms=(time.monotonic() - started) * 1000,
            )
        requires_approval = command_requires_approval(argv)
        if requires_approval and not approved:
            return self._result(
                command=command_text,
                argv=argv,
                cwd=target,
                status="approval_required",
                exit_code=None,
                stderr="explicit approval is required for destructive/package/remote commands",
                duration_ms=(time.monotonic() - started) * 1000,
                requires_approval=True,
            )
        execution_argv = argv
        execution_cwd: str | None = str(target)
        if self.profile.executor == "docker":
            execution_argv = self._docker_argv(argv, target, network)
            execution_cwd = None
        elif self.profile.id == "local_restricted":
            try:
                execution_argv = self._local_restricted_argv(argv, target)
            except CommandPolicyError as exc:
                return self._result(
                    command=command_text,
                    argv=argv,
                    cwd=target,
                    status="denied",
                    exit_code=None,
                    stderr=str(exc),
                    duration_ms=(time.monotonic() - started) * 1000,
                )
            execution_cwd = None
        try:
            completed = subprocess.run(
                execution_argv,
                cwd=execution_cwd,
                env=_safe_environment(env),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return self._result(
                command=command_text,
                argv=argv,
                cwd=target,
                status="timeout",
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                duration_ms=(time.monotonic() - started) * 1000,
                timed_out=True,
            )
        except OSError as exc:
            return self._result(
                command=command_text,
                argv=argv,
                cwd=target,
                status="failed",
                exit_code=None,
                stderr=f"unable to execute command: {exc}",
                duration_ms=(time.monotonic() - started) * 1000,
            )
        return self._result(
            command=command_text,
            argv=argv,
            cwd=target,
            status="passed" if completed.returncode == 0 else "failed",
            exit_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_ms=(time.monotonic() - started) * 1000,
        )


__all__ = [
    "CommandPolicyError",
    "CommandRunner",
    "command_may_use_network",
    "command_requires_approval",
    "parse_command",
    "redact_text",
]
