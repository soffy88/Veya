"""Canonical permission decision engine.

The engine classifies normalized effects and scopes.  Adapters may translate
their native request into :class:`OperationContext`, but they must not invent a
second approval policy.  This module deliberately contains no executor or
sandbox code: it only returns a decision and an auditable reason.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .executor_registry import ExecutorRuntimeIdentity


class Decision(StrEnum):
    ALLOW = "ALLOW"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DENY = "DENY"


class Scope(StrEnum):
    PROJECT = "PROJECT"
    USER = "USER"
    HOST = "HOST"
    REMOTE = "REMOTE"
    OUTSIDE_ALLOWED_SCOPE = "OUTSIDE_ALLOWED_SCOPE"


class ReasonCode(StrEnum):
    ALLOW_READ_ONLY = "ALLOW_READ_ONLY"
    ALLOW_PROJECT_MUTATION = "ALLOW_PROJECT_MUTATION"
    ALLOW_PROJECT_GIT = "ALLOW_PROJECT_GIT"
    ALLOW_USER_RUNTIME = "ALLOW_USER_RUNTIME"
    ALLOW_NETWORK = "ALLOW_NETWORK"
    ALLOW_WORKER = "ALLOW_WORKER"
    APPROVAL_HOST_PRIVILEGE = "APPROVAL_HOST_PRIVILEGE"
    APPROVAL_HOST_DESTRUCTIVE = "APPROVAL_HOST_DESTRUCTIVE"
    APPROVAL_SECURITY_BOUNDARY = "APPROVAL_SECURITY_BOUNDARY"
    APPROVAL_IRREVERSIBLE_REMOTE = "APPROVAL_IRREVERSIBLE_REMOTE"
    APPROVAL_UNKNOWN_HIGH_IMPACT = "APPROVAL_UNKNOWN_HIGH_IMPACT"
    DENY_SCOPE_ESCAPE = "DENY_SCOPE_ESCAPE"
    DENY_INVALID_APPROVAL = "DENY_INVALID_APPROVAL"
    DENY_AUTHORITY_VIOLATION = "DENY_AUTHORITY_VIOLATION"
    DENY_PATH_TRAVERSAL = "DENY_PATH_TRAVERSAL"


@dataclass(frozen=True)
class OperationContext:
    """Normalized effect description shared by every execution layer."""

    actor: str = ""
    tool: str = ""
    operation: str = ""
    workspace_root: Path | None = None
    cwd: Path | None = None
    target_paths: tuple[Path, ...] = ()
    command: tuple[str, ...] | None = None
    filesystem_effect: str = "none"
    process_effect: str = "none"
    network_effect: str = "none"
    git_effect: str = "none"
    service_effect: str = "none"
    privilege_level: str = "user"
    reversibility: str = "reversible"
    remote_effect: str = "none"
    execution_id: str | None = None
    session_id: str | None = None
    goal_run_id: str | None = None
    executor_identity: ExecutorRuntimeIdentity | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PermissionDecision:
    decision: Decision
    reason: ReasonCode
    scope: Scope
    effects: tuple[str, ...] = ()
    normalized_operation: str = ""
    details: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW


_HOST_ROOTS = tuple(Path(item) for item in ("/etc", "/usr", "/boot", "/dev", "/proc", "/sys"))
_DESTRUCTIVE_WORDS = frozenset({"mkfs", "fdisk", "parted", "mount", "umount", "iptables", "nft"})
_READ_ONLY_GIT = frozenset({"status", "diff", "log", "show", "rev-parse", "ls-files"})
_REMOTE_REVERSIBLE_GIT = frozenset({"fetch", "pull", "ls-remote"})
_LOCAL_GIT = frozenset(
    {
        "add",
        "commit",
        "branch",
        "switch",
        "checkout",
        "restore",
        "stash",
        "merge",
        "rebase",
        "cherry-pick",
        "reset",
        "tag",
        "worktree",
        "gc",
        "repack",
    }
)


def _canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _scope(context: OperationContext) -> Scope:
    root = _canonical(context.workspace_root) if context.workspace_root else None
    targets = tuple(_canonical(path) for path in context.target_paths)
    if any(path == host or host in path.parents for path in targets for host in _HOST_ROOTS):
        return Scope.HOST
    if root and targets:
        if all(path == root or root in path.parents for path in targets):
            return Scope.PROJECT
        return Scope.OUTSIDE_ALLOWED_SCOPE
    if context.service_effect == "user" or context.privilege_level == "user":
        return Scope.USER
    if context.remote_effect != "none" or context.network_effect != "none":
        return Scope.REMOTE
    return Scope.OUTSIDE_ALLOWED_SCOPE


def _command_words(command: Iterable[str] | None) -> tuple[str, ...]:
    if not command:
        return ()
    return tuple(
        Path(item).name.lower() if index == 0 else item.lower()
        for index, item in enumerate(command)
    )


class PermissionEngine:
    """Single authority for ALLOW / APPROVAL_REQUIRED / DENY."""

    def evaluate(self, context: OperationContext) -> PermissionDecision:
        scope = _scope(context)
        words = _command_words(context.command)
        executable = words[0] if words else ""
        operation = context.operation.lower()
        effects = tuple(
            value
            for value in (
                context.filesystem_effect,
                context.process_effect,
                context.network_effect,
                context.git_effect,
                context.service_effect,
                context.remote_effect,
            )
            if value != "none"
        )

        if scope == Scope.OUTSIDE_ALLOWED_SCOPE:
            return PermissionDecision(
                Decision.DENY, ReasonCode.DENY_SCOPE_ESCAPE, scope, effects, operation
            )
        if scope == Scope.HOST and context.filesystem_effect not in {"read", "none"}:
            return PermissionDecision(
                Decision.APPROVAL_REQUIRED,
                ReasonCode.APPROVAL_HOST_PRIVILEGE,
                scope,
                effects,
                operation,
            )
        if (
            any(path.name == ".env" for path in context.target_paths)
            and context.filesystem_effect != "read"
        ):
            return PermissionDecision(
                Decision.APPROVAL_REQUIRED,
                ReasonCode.APPROVAL_SECURITY_BOUNDARY,
                scope,
                effects,
                operation,
            )
        home = Path.home().resolve()
        if any(
            path == protected or protected in path.parents
            for path in tuple(_canonical(path) for path in context.target_paths)
            for protected in (home / ".ssh", home / ".gnupg", home / ".aws")
        ):
            return PermissionDecision(
                Decision.DENY,
                ReasonCode.DENY_AUTHORITY_VIOLATION,
                Scope.USER,
                effects,
                operation,
            )
        if executable == "sudo" or context.privilege_level in {"root", "host"}:
            return PermissionDecision(
                Decision.APPROVAL_REQUIRED,
                ReasonCode.APPROVAL_HOST_PRIVILEGE,
                Scope.HOST,
                effects,
                operation,
            )
        if executable in {"sh", "bash", "zsh"} and any(
            flag in words[1:] for flag in ("-c", "-lc", "-cl")
        ):
            # A wrapper is safe only when its inner command is still inside the
            # same normalized effect model.  These tokens are command-level
            # boundaries, not an approval bypass; the full argv remains in the
            # operation fingerprint at the caller.
            flag_index = next(
                index
                for index, token in enumerate(words[1:], start=1)
                if token in {"-c", "-lc", "-cl"}
            )
            command = context.command or ()
            try:
                inner = set(shlex.split(" ".join(command[flag_index + 1 :])))
            except ValueError:
                inner = set()
            if {"sudo", "su", "mkfs", "fdisk", "parted", "mount", "umount"} & inner:
                return PermissionDecision(
                    Decision.APPROVAL_REQUIRED,
                    ReasonCode.APPROVAL_HOST_PRIVILEGE,
                    Scope.HOST,
                    effects,
                    operation,
                )
            if "systemctl" in inner and "--user" not in inner:
                return PermissionDecision(
                    Decision.APPROVAL_REQUIRED,
                    ReasonCode.APPROVAL_HOST_PRIVILEGE,
                    Scope.HOST,
                    effects,
                    operation,
                )
            if "--force" in inner or "--force-with-lease" in inner:
                return PermissionDecision(
                    Decision.APPROVAL_REQUIRED,
                    ReasonCode.APPROVAL_IRREVERSIBLE_REMOTE,
                    Scope.REMOTE,
                    effects,
                    operation,
                )
        if executable in _DESTRUCTIVE_WORDS or context.reversibility in {
            "irreversible",
            "destructive",
        }:
            return PermissionDecision(
                Decision.APPROVAL_REQUIRED,
                ReasonCode.APPROVAL_HOST_DESTRUCTIVE,
                scope,
                effects,
                operation,
            )
        if context.service_effect == "system":
            return PermissionDecision(
                Decision.APPROVAL_REQUIRED,
                ReasonCode.APPROVAL_HOST_PRIVILEGE,
                Scope.HOST,
                effects,
                operation,
            )
        if context.remote_effect in {"force", "delete", "rewrite", "destroy"}:
            return PermissionDecision(
                Decision.APPROVAL_REQUIRED,
                ReasonCode.APPROVAL_IRREVERSIBLE_REMOTE,
                Scope.REMOTE,
                effects,
                operation,
            )
        if words[:1] == ("git",):
            git_operation = next((word for word in words[1:] if not word.startswith("-")), "")
            if git_operation in {"push"} and any(
                flag in words[1:] for flag in ("--force", "-f", "--force-with-lease")
            ):
                return PermissionDecision(
                    Decision.APPROVAL_REQUIRED,
                    ReasonCode.APPROVAL_IRREVERSIBLE_REMOTE,
                    Scope.REMOTE,
                    effects,
                    operation,
                )
            if git_operation in _READ_ONLY_GIT:
                return PermissionDecision(
                    Decision.ALLOW, ReasonCode.ALLOW_READ_ONLY, scope, effects, operation
                )
            if (
                git_operation in _LOCAL_GIT
                or git_operation in _REMOTE_REVERSIBLE_GIT
                or git_operation == "push"
            ):
                return PermissionDecision(
                    Decision.ALLOW, ReasonCode.ALLOW_PROJECT_GIT, scope, effects, operation
                )
        if context.service_effect == "user":
            return PermissionDecision(
                Decision.ALLOW, ReasonCode.ALLOW_USER_RUNTIME, Scope.USER, effects, operation
            )
        if context.network_effect != "none":
            return PermissionDecision(
                Decision.ALLOW, ReasonCode.ALLOW_NETWORK, Scope.REMOTE, effects, operation
            )
        if operation.startswith("worker.") or context.tool in {
            "worker.dispatch",
            "hicode",
            "pi",
            "codex",
            "agy",
        }:
            return PermissionDecision(
                Decision.ALLOW, ReasonCode.ALLOW_WORKER, scope, effects, operation
            )
        if context.filesystem_effect in {"read", "none"} and context.process_effect in {
            "none",
            "inspect",
        }:
            return PermissionDecision(
                Decision.ALLOW, ReasonCode.ALLOW_READ_ONLY, scope, effects, operation
            )
        if scope == Scope.PROJECT or scope == Scope.USER:
            return PermissionDecision(
                Decision.ALLOW, ReasonCode.ALLOW_PROJECT_MUTATION, scope, effects, operation
            )
        return PermissionDecision(
            Decision.APPROVAL_REQUIRED,
            ReasonCode.APPROVAL_UNKNOWN_HIGH_IMPACT,
            scope,
            effects,
            operation,
        )


def parse_command_context(
    command: str | Iterable[str], *, cwd: Path, workspace_root: Path
) -> OperationContext:
    """Build a normalized context for compatibility callers."""

    argv = tuple(shlex.split(command) if isinstance(command, str) else command)
    executable = Path(argv[0]).name if argv else ""
    service_effect = (
        "user"
        if executable == "systemctl" and "--user" in argv
        else "system"
        if executable == "systemctl"
        else "none"
    )
    return OperationContext(
        tool="shell.exec",
        operation="shell.exec",
        workspace_root=workspace_root,
        cwd=cwd,
        command=argv,
        filesystem_effect="read"
        if argv and argv[0] in {"pwd", "ls", "cat", "rg", "grep", "find"}
        else "none"
        if executable == "systemctl"
        else "write",
        network_effect="network" if argv and Path(argv[0]).name in {"curl", "wget"} else "none",
        git_effect="command" if argv and Path(argv[0]).name == "git" else "none",
        service_effect=service_effect,
    )


__all__ = [
    "Decision",
    "OperationContext",
    "PermissionDecision",
    "PermissionEngine",
    "ReasonCode",
    "Scope",
    "parse_command_context",
]
