"""Safe, evidence-producing command execution for coding worktrees."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

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
_SANDBOX_DEPTH_ENV = "VEYA_SANDBOX_DEPTH"


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


def _safe_environment(
    extra: Mapping[str, str] | None,
    runtime_profile: Any | None = None,
) -> dict[str, str]:
    """Keep useful process settings while excluding credential-shaped values."""
    allowed = {
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TMPDIR",
        "HOME",
        "USER",
        _SANDBOX_DEPTH_ENV,
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    if runtime_profile is not None:
        path_entries = getattr(runtime_profile, "path_entries", [])
        if path_entries:
            current_path = environment.get("PATH", os.defpath)
            new_entries = [p for p in path_entries if p and p not in current_path.split(os.pathsep)]
            if new_entries:
                environment["PATH"] = os.pathsep.join([*new_entries, current_path])
        venv_root = getattr(runtime_profile, "venv_root", None)
        if venv_root:
            environment["VIRTUAL_ENV"] = str(venv_root)
        if getattr(runtime_profile, "goroot", None):
            environment["GOROOT"] = str(runtime_profile.goroot)
        if getattr(runtime_profile, "gopath", None):
            environment["GOPATH"] = str(runtime_profile.gopath)
        if getattr(runtime_profile, "gobin", None):
            environment["GOBIN"] = str(runtime_profile.gobin)
        library_paths = getattr(runtime_profile, "library_paths", [])
        if library_paths:
            existing_ld = environment.get("LD_LIBRARY_PATH")
            environment["LD_LIBRARY_PATH"] = os.pathsep.join(
                [*library_paths, *(existing_ld.split(os.pathsep) if existing_ld else [])]
            )
        environment["PYTHONNOUSERSITE"] = "1"
    for key, value in (extra or {}).items():
        if _SECRET_NAME.search(key):
            continue
        environment[str(key)] = str(value)
    # The marker is injected only by our bubblewrap wrapper.  Preserve the
    # inherited marker even if a caller supplied a conflicting extra value so
    # a nested coding harness cannot accidentally escape the parent sandbox.
    if os.environ.get(_SANDBOX_DEPTH_ENV) == "1":
        environment[_SANDBOX_DEPTH_ENV] = "1"
    return environment


def _within(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def _executable_mount_hops(argv: Sequence[str], cwd: Path) -> list[str]:
    """Parent dirs that must stay visible for ``argv[0]`` to exec (P0-D).

    Returns the parent of every symlink component in the (unresolved)
    executable path plus the resolved binary's own parent. Entries outside
    the private roots are harmless: the caller only mounts paths below
    ``/home``/``/root``/``/run``.
    """

    if not argv:
        return []
    raw = str(argv[0])
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        if "/" not in raw and "\\" not in raw:
            return []
        candidate = cwd / candidate
    hops: list[str] = []
    try:
        absolute = Path(os.path.abspath(candidate))
        # Walk the full symlink resolution chain: every symlink component —
        # in the literal path and in each link target — needs its parent
        # directory visible, otherwise exec fails with ENOENT once the
        # private roots above become tmpfs.
        current: Path | None = absolute
        for _ in range(16):
            if current is None or current == Path("/"):
                break
            chain: list[Path] = [current, *list(current.parents)]
            next_link: Path | None = None
            for prefix in chain:
                if prefix == Path("/"):
                    break
                try:
                    if prefix.is_symlink():
                        hops.append(str(prefix.parent))
                        if next_link is None and prefix == current:
                            try:
                                target = Path(os.readlink(prefix))
                            except OSError:
                                target = None
                            if target is not None:
                                current = target if target.is_absolute() else prefix.parent / target
                                next_link = current
                except OSError:
                    continue
            if next_link is None:
                break
        with contextlib.suppress(OSError):
            hops.append(str(absolute.resolve().parent))
    except (OSError, ValueError):
        pass
    return hops


# Bare tool names resolved through the canonical WorkspaceRuntimeProfile.
# CODE runs in the (possibly venv-less) worktree while RUNTIME comes from the
# canonical project root, so a relative ``venv/bin/python`` or a bare
# ``python``/``pytest`` must resolve to the discovered absolute binary instead
# of failing with ENOENT inside the (sandboxed) worktree cwd.
_INTERPRETER_PROFILE_ATTRS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("python", "python3"), "python_bin"),
    (("pytest",), "pytest_bin"),
    (("ruff",), "ruff_bin"),
    (("mypy",), "mypy_bin"),
    (("node",), "node_bin"),
    (("npm",), "npm"),
    (("npx",), "npx"),
    (("pnpm",), "pnpm"),
    (("yarn",), "yarn"),
    (("bun",), "bun"),
    (("uv",), "uv"),
    (("pip", "pip3"), "pip"),
    (("go",), "go"),
    (("cargo",), "cargo"),
)


def _profile_bin(runtime_profile: Any | None, attr: str) -> str | None:
    if runtime_profile is None:
        return None
    try:
        value = getattr(runtime_profile, attr, None)
    except Exception:
        return None
    if not value or not isinstance(value, str):
        return None
    candidate = Path(value)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _profile_bin_for_name(runtime_profile: Any | None, name: str) -> str | None:
    for names, attr in _INTERPRETER_PROFILE_ATTRS:
        if name in names:
            return _profile_bin(runtime_profile, attr)
    return None


def _venv_bin(runtime_profile: Any | None, name: str) -> str | None:
    """Return the venv-relative binary path WITHOUT resolving symlinks.

    A venv interpreter (e.g. ``.venv/bin/python`` -> uv-managed python) must
    keep its venv path so CPython finds ``pyvenv.cfg``/``lib`` relative to
    ``argv[0]`` — inside a bwrap sandbox the resolved target's siblings may
    be hidden (``--tmpfs /home``) while the venv itself stays visible through
    the workspace bind. The existence/executability check still follows the
    link, so a dangling link is never returned.
    """

    if runtime_profile is None:
        return None
    try:
        venv_root = getattr(runtime_profile, "venv_root", None)
    except Exception:
        return None
    if not venv_root:
        return None
    aliases: list[str] = [name]
    if name == "python":
        aliases.append("python3")
    elif name == "python3":
        aliases.append("python")
    for alias in aliases:
        candidate = Path(str(venv_root)) / "bin" / alias
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


def resolve_interpreter_argv(
    argv: list[str],
    cwd: Path,
    runtime_profile: Any | None = None,
) -> list[str]:
    """Resolve a worktree-relative/bare interpreter to its canonical absolute binary.

    Only rewrites when the literal ``argv[0]`` would NOT execute from ``cwd``
    (relative path missing there, or bare name unresolvable via ``PATH``) and
    the runtime profile provides a verified executable binary for that tool.
    Otherwise returns ``argv`` unchanged, preserving worktree-local venv
    precedence and all existing error behaviour.
    """

    if not argv or runtime_profile is None:
        return argv
    raw = argv[0]
    name = Path(raw).name.lower()
    # Prefer the venv-relative path (symlink preserved) so the interpreter
    # keeps its venv context under filesystem isolation; fall back to the
    # discovered absolute binary for non-venv tools.
    resolved = _venv_bin(runtime_profile, name) or _profile_bin_for_name(runtime_profile, name)
    if not resolved:
        return argv
    if "/" in raw or "\\" in raw:
        candidate = Path(raw).expanduser()
        absolute = candidate if candidate.is_absolute() else cwd / candidate
        try:
            if absolute.is_file() and os.access(absolute, os.X_OK):
                return argv
        except OSError:
            pass
        return [resolved, *argv[1:]]
    if shutil.which(raw):
        return argv
    return [resolved, *argv[1:]]


def _nested_write_path_violation(argv: list[str], root: Path, cwd: Path) -> str | None:
    """Reject explicit write targets outside the already-isolated worktree.

    A managed coding runtime may already be inside its outer sandbox, where a
    second namespace cannot be created.  Keep the important write boundary for
    commands that pass their target path explicitly; ordinary relative writes
    remain confined by the inherited outer sandbox and worktree cwd.
    """
    if os.environ.get(_SANDBOX_DEPTH_ENV) != "1":
        return None
    command_text = " ".join(argv[1:]).lower()
    if not re.search(r"\b(?:open|write|touch|mkdir|unlink|remove|rename|replace)\b", command_text):
        return None
    for index, item in enumerate(argv[1:], start=1):
        if index > 1 and argv[index - 1] in {"-c", "--command", "-e"}:
            continue
        if item.startswith("-") or ("/" not in item and "\\" not in item):
            continue
        candidate = Path(item).expanduser()
        resolved = candidate if candidate.is_absolute() else cwd / candidate
        if not _within(root, resolved.resolve(strict=False)):
            return f"explicit write path must remain inside the task workspace: {item}"
    return None


class CommandRunner:
    """Run argv commands in a worktree and capture a redacted result artifact."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        profile: str | SandboxProfile = "local_restricted",
        artifact_root: str | Path | None = None,
        runtime_profile: Any | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        if not self.workspace_root.is_dir():
            raise CommandPolicyError(f"workspace root is not a directory: {self.workspace_root}")
        self.profile = get_sandbox_profile(profile)
        self.artifact_root = Path(artifact_root).expanduser().resolve() if artifact_root else None
        if self.artifact_root and not _within(self.workspace_root, self.artifact_root):
            raise CommandPolicyError("command artifacts must stay inside the workspace root")
        self.runtime_profile = runtime_profile

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
            value for key, value in os.environ.items() if _SECRET_NAME.search(key) and value
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
            artifact.write_text(
                json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
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

    def _local_restricted_argv(
        self,
        argv: list[str],
        cwd: Path,
        runtime_profile: Any | None = None,
    ) -> list[str]:
        """Use bubblewrap when available so local restricted means real isolation."""
        bubblewrap = shutil.which("bwrap")
        if not bubblewrap:
            raise CommandPolicyError(
                "local_restricted requires bubblewrap for filesystem/network isolation"
            )
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
        rp = runtime_profile or self.runtime_profile
        if rp is not None:
            mounted_entries: set[str] = set()
            venv_root = getattr(rp, "venv_root", None)
            entries = list(getattr(rp, "path_entries", []))
            if venv_root:
                entries.append(str(venv_root))
            python_bin = getattr(rp, "python_bin", None)
            if python_bin:
                entries.append(str(Path(python_bin).parent))
            goroot = getattr(rp, "goroot", None)
            if goroot:
                entries.append(str(goroot))
            gopath = getattr(rp, "gopath", None)
            if gopath:
                entries.append(str(gopath))
            # The executed binary itself may sit behind intermediate symlinks
            # (e.g. ``.venv/bin/python`` -> uv toolchain with aliased install
            # dirs). Mount every symlink hop's parent so exec resolution keeps
            # working once /home//root//run become tmpfs above.
            entries.extend(_executable_mount_hops(argv, cwd))
            for entry in entries:
                if not entry:
                    continue
                p = Path(entry).resolve()
                if p.exists() and any(
                    _within(Path(priv), p) for priv in ("/home", "/root", "/run")
                ):
                    p_str = str(p)
                    if p_str not in mounted_entries:
                        wrapped.extend(["--ro-bind", p_str, p_str])
                        mounted_entries.add(p_str)
        wrapped.extend(
            [
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
                _SANDBOX_DEPTH_ENV,
                "1",
                "--setenv",
                "PYTHONNOUSERSITE",
                "1",
            ]
        )
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
        command_text = (
            command if isinstance(command, str) else " ".join(shlex.quote(item) for item in argv)
        )
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
        write_path_violation = _nested_write_path_violation(argv, self.workspace_root, target)
        if write_path_violation:
            return self._result(
                command=command_text,
                argv=argv,
                cwd=target,
                status="failed",
                exit_code=None,
                stderr=write_path_violation,
                duration_ms=(time.monotonic() - started) * 1000,
            )
        execution_argv = argv
        execution_cwd: str | None = str(target)
        rp = self.runtime_profile
        if Path(argv[0]).name == "pytest":
            pytest_bin = _profile_bin(rp, "pytest_bin")
            python_bin = _venv_bin(rp, "python") or _profile_bin(rp, "python_bin")
            if pytest_bin:
                execution_argv = [pytest_bin, *argv[1:]]
            elif python_bin:
                execution_argv = [python_bin, "-m", "pytest", *argv[1:]]
            elif importlib.util.find_spec("pytest"):
                execution_argv = [sys.executable, "-m", "pytest", *argv[1:]]
        else:
            # CODE runs in the (possibly venv-less) worktree while RUNTIME
            # comes from the canonical project root: resolve a
            # worktree-relative/bare interpreter (``venv/bin/python``,
            # ``python3``, ``ruff`` ...) to its verified absolute binary.
            execution_argv = resolve_interpreter_argv(argv, target, rp)
        if self.profile.executor == "docker":
            execution_argv = self._docker_argv(argv, target, network)
            execution_cwd = None
        elif (
            self.profile.id in ("local_restricted", "l0_isolated")
            and os.environ.get(_SANDBOX_DEPTH_ENV) != "1"
        ):
            try:
                execution_argv = self._local_restricted_argv(execution_argv, target, rp)
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
            environment = _safe_environment(env, rp)
            if not rp:
                for venv_name in (".venv", "venv"):
                    workspace_bin = self.workspace_root / venv_name / "bin"
                    if workspace_bin.is_dir():
                        environment["PATH"] = os.pathsep.join(
                            [str(workspace_bin), environment.get("PATH", os.defpath)]
                        )
                        break
            python_paths = [str(self.workspace_root)]
            for sub in ("obase", "oprim", "omodul", "oskill", "oservi"):
                sub_p = self.workspace_root / "platform" / "3O" / sub
                if sub_p.is_dir():
                    python_paths.append(str(sub_p))
            existing_pythonpath = environment.get("PYTHONPATH")
            if existing_pythonpath:
                python_paths.append(existing_pythonpath)
            environment["PYTHONPATH"] = os.pathsep.join(python_paths)
            completed = subprocess.run(
                execution_argv,
                cwd=execution_cwd,
                env=environment,
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
    "resolve_interpreter_argv",
]
