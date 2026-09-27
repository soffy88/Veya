"""Canonical permission decision engine.

The engine classifies normalized effects and scopes.  Adapters may translate
their native request into :class:`OperationContext`, but they must not invent a
second approval policy.  This module deliberately contains no executor or
sandbox code: it only returns a decision and an auditable reason.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Sequence
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


class CommandEffect(StrEnum):
    """Canonical semantics of one command line.

    This is the single classification every caller consumes.  It exists so the
    meaning of a command survives argv parsing instead of being lost: the
    previous compatibility path only kept a coarse ``filesystem_effect`` and no
    targets, which made a destructive command look like an ordinary user-level
    write and therefore ``ALLOW``.
    """

    NONE = "NONE"
    READ_ONLY = "READ_ONLY"
    REVERSIBLE_MUTATION = "REVERSIBLE_MUTATION"
    DESTRUCTIVE_MUTATION = "DESTRUCTIVE_MUTATION"
    PRIVILEGED_HOST_MUTATION = "PRIVILEGED_HOST_MUTATION"
    REMOTE_MUTATION = "REMOTE_MUTATION"
    REMOTE_IRREVERSIBLE_MUTATION = "REMOTE_IRREVERSIBLE_MUTATION"


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
    # Canonical command semantics. ``parse_command_context`` is its only
    # producer; every decision reads it, so no caller can re-derive (and
    # downgrade) a command's meaning on its own.
    command_effect: str = str(CommandEffect.NONE)
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


# ── command semantics ─────────────────────────────────────────────────────
# One vocabulary, consulted by :func:`classify_command`.  Nothing below makes a
# decision; it only describes what a command *does*.

# Irreversible loss of existing data. ``rm``/``chmod``/... belong here because a
# project-scoped ``rm -rf`` is still destructive; scope and danger are
# orthogonal and are judged separately.
_DESTRUCTIVE_EXECUTABLES = frozenset(
    {
        "rm",
        "rmdir",
        "shred",
        "unlink",
        "dd",
        "chmod",
        "chown",
        "chgrp",
        "truncate",
        "mkfs",
        "fdisk",
        "parted",
        "wipefs",
        "mkswap",
    }
)

# Host-level state: block devices, kernel/network rules, service manager.
_HOST_MUTATION_EXECUTABLES = frozenset(
    {
        "mount",
        "umount",
        "losetup",
        "iptables",
        "ip6tables",
        "nft",
        "insmod",
        "rmmod",
        "modprobe",
        "systemctl",
    }
)

# Signals reach arbitrary processes, including init.
_SIGNAL_EXECUTABLES = frozenset({"kill", "killall", "pkill"})

_READ_ONLY_EXECUTABLES = frozenset(
    {
        "ls",
        "cat",
        "echo",
        "pwd",
        "head",
        "tail",
        "wc",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "stat",
        "file",
        "which",
        "whoami",
        "id",
        "date",
        "env",
        "printenv",
        "diff",
        "tree",
        "du",
        "df",
        "ps",
        "sleep",
        "true",
        "false",
        "basename",
        "dirname",
        "realpath",
        "seq",
        "sort",
        "uniq",
        "cut",
        "tr",
        "jq",
        "python",
        "python3",
        "node",
        "pytest",
        "ruff",
        "mypy",
        "tsc",
        "eslint",
        "curl",
        "wget",
    }
)

# Ordinary reversible project mutations.
_MUTATING_EXECUTABLES = frozenset(
    {
        "mkdir",
        "touch",
        "cp",
        "mv",
        "ln",
        "tar",
        "unzip",
        "patch",
        "install",
        "rsync",
    }
)

_PACKAGE_MANAGERS = frozenset(
    {"pip", "pip3", "npm", "npx", "pnpm", "yarn", "bun", "cargo", "go", "uv", "poetry", "gem"}
)
_PACKAGE_GLOBAL_FLAGS = frozenset(
    {"-g", "--global", "--system", "--global-root", "--prefix-global", "--os", "--deny-scripts"}
)
_PACKAGE_MUTATING_SUBCOMMANDS = frozenset(
    {"install", "add", "remove", "uninstall", "update", "upgrade", "get", "i", "rm", "sync"}
)

_GIT_READ_ONLY = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "rev-parse",
        "rev-list",
        "blame",
        "describe",
        "shortlog",
        "ls-files",
        "ls-tree",
        "ls-remote",
        "cat-file",
        "symbolic-ref",
        "reflog",
        "for-each-ref",
        "count-objects",
        "verify-commit",
        "check-ignore",
        "grep",
        "whatchanged",
    }
)
# Reversible project-scoped Git mutation: intentionally still open.
_GIT_NORMAL_MUTATION = frozenset(
    {"add", "commit", "init", "merge", "switch", "checkout", "branch", "tag", "remote", "mv"}
)
_GIT_DESTRUCTIVE = frozenset(
    {"clean", "reset", "restore", "rebase", "filter-branch", "gc", "prune", "worktree", "stash"}
)
_GIT_REMOTE = frozenset({"clone", "fetch", "pull", "push", "submodule", "remote"})
# Flags that turn an otherwise normal Git operation destructive.
_GIT_DESTRUCTIVE_FLAGS = frozenset(
    {"--hard", "-d", "-D", "--delete", "--force", "-f", "--orphan", "--prune", "--aggressive"}
)
# Network reachers whose primary effect is a remote fetch.
_NETWORK_FETCH_EXECUTABLES = frozenset({"curl", "wget"})


def _git_subcommand(argv: Sequence[str]) -> tuple[str, list[str]]:
    """Return the Git subcommand and its arguments (global flags skipped)."""
    for index, item in enumerate(argv[1:], start=1):
        if item.startswith("-"):
            continue
        return item.lower(), [value.lower() for value in argv[index + 1 :]]
    return "", []


def _package_scope(args: Sequence[str]) -> str:
    """``global`` / ``user`` / ``project`` destination for a package command."""
    for item in args:
        if item in _PACKAGE_GLOBAL_FLAGS:
            return "global"
        if item.startswith("--prefix=") or item == "--prefix":
            return "global"
    if any(item in {"--user", "--user-install"} for item in args):
        return "user"
    return "project"


def _positional_targets(argv: Sequence[str], *, skip_options: bool = True) -> list[str]:
    """Non-flag operands: the paths a command is being pointed at."""
    targets: list[str] = []
    skip_next = False
    for item in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if item == "--":
            continue
        if item.startswith("-"):
            if skip_options and item in {"-o", "--output", "-d", "--directory", "-C"}:
                skip_next = True
            continue
        # ``dd`` style key=value targets are resolved by the caller.
        if "=" in item and item.split("=", 1)[0] in {"of", "if", "output"}:
            continue
        targets.append(item)
    return targets


def classify_command(
    argv: Sequence[str], *, cwd: Path
) -> tuple[CommandEffect, tuple[Path, ...], str, str, str]:
    """Describe one command line.

    Returns ``(effect, target_paths, filesystem_effect, network_effect,
    reversibility)``.  Classification is argument-aware: ``rm file`` and
    ``rm -rf /`` are both destructive but resolve to different targets, and
    ``git push`` differs from ``git push --force``.
    """
    words = _command_words(argv)
    if not words:
        return CommandEffect.NONE, (), "none", "none", "reversible"
    executable = words[0]
    raw_argv = [str(item) for item in argv]

    def _resolve(values: Iterable[str]) -> tuple[Path, ...]:
        resolved: list[Path] = []
        for value in values:
            candidate = Path(value).expanduser()
            resolved.append(candidate if candidate.is_absolute() else cwd / candidate)
        return tuple(resolved)

    if executable == "sudo":
        inner = _command_words(raw_argv[1:])
        inner_effect = (
            classify_command(raw_argv[1:], cwd=cwd)[0] if raw_argv[1:] else CommandEffect.NONE
        )
        effect = (
            CommandEffect.PRIVILEGED_HOST_MUTATION
            if inner_effect
            in {
                CommandEffect.DESTRUCTIVE_MUTATION,
                CommandEffect.PRIVILEGED_HOST_MUTATION,
                CommandEffect.REMOTE_IRREVERSIBLE_MUTATION,
                CommandEffect.REMOTE_MUTATION,
            }
            else CommandEffect.REVERSIBLE_MUTATION
        )
        return (
            effect,
            (),
            "write",
            "network" if inner and inner[0] in _NETWORK_FETCH_EXECUTABLES else "none",
            "irreversible" if effect is CommandEffect.PRIVILEGED_HOST_MUTATION else "reversible",
        )

    if executable == "systemctl":
        user_scoped = "--user" in words
        if user_scoped:
            return CommandEffect.REVERSIBLE_MUTATION, (), "none", "none", "reversible"
        return CommandEffect.PRIVILEGED_HOST_MUTATION, (), "write", "none", "destructive"

    if executable in _DESTRUCTIVE_EXECUTABLES or any(
        item == executable or item.startswith(f"{executable}.")
        for item in ("mkfs", "mkfs.ext4", "mkfs.xfs", "mkfs.btrfs")
    ):
        if executable == "dd":
            # dd writes the ``of=`` operand, which is frequently a device.
            devices = [
                item.split("=", 1)[1]
                for item in raw_argv[1:]
                if item.startswith("of=") or item.startswith("if=")
            ]
            return (
                CommandEffect.DESTRUCTIVE_MUTATION,
                _resolve(devices or _positional_targets(raw_argv)),
                "write",
                "none",
                "destructive",
            )
        return (
            CommandEffect.DESTRUCTIVE_MUTATION,
            _resolve(_positional_targets(raw_argv)),
            "write",
            "none",
            "destructive",
        )

    if executable in _SIGNAL_EXECUTABLES:
        return CommandEffect.DESTRUCTIVE_MUTATION, (), "write", "none", "destructive"

    if executable == "find":
        destructive_args = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls"}
        if any(item in destructive_args for item in words[1:]):
            return (
                CommandEffect.DESTRUCTIVE_MUTATION,
                _resolve(_positional_targets(raw_argv)),
                "write",
                "none",
                "destructive",
            )
        return (
            CommandEffect.READ_ONLY,
            _resolve(_positional_targets(raw_argv)),
            "read",
            "none",
            "reversible",
        )

    if executable in _HOST_MUTATION_EXECUTABLES:
        return CommandEffect.PRIVILEGED_HOST_MUTATION, (), "write", "none", "destructive"
    if executable == "git":
        subcommand, args = _git_subcommand(raw_argv)
        flags = {item for item in args}
        # ONE canonical Git policy: the vocabulary the engine has always decided
        # with. Deriving the semantics from these same sets keeps Git from
        # acquiring a second, divergent policy.
        if subcommand in _READ_ONLY_GIT:
            return CommandEffect.READ_ONLY, (), "read", "none", "reversible"
        if subcommand == "push":
            irreversible = bool({"--force", "-f", "--force-with-lease"} & flags)
            return (
                (
                    CommandEffect.REMOTE_IRREVERSIBLE_MUTATION
                    if irreversible
                    else CommandEffect.REMOTE_MUTATION
                ),
                (),
                "write",
                "network",
                "irreversible" if irreversible else "reversible",
            )
        if subcommand == "clone" or subcommand in _REMOTE_REVERSIBLE_GIT:
            return CommandEffect.REMOTE_MUTATION, (), "write", "network", "reversible"
        # Discards untracked/ignored work, working-tree edits, or unreachable
        # objects, and is in no allow set.
        if subcommand in {"clean", "restore", "gc", "prune", "filter-branch"}:
            return CommandEffect.DESTRUCTIVE_MUTATION, (), "write", "none", "destructive"
        # ``reset`` stays reversible except for the history-destroying form.
        if subcommand == "reset" and "--hard" in flags:
            return CommandEffect.DESTRUCTIVE_MUTATION, (), "write", "none", "destructive"
        # Destructive *flags* on otherwise-normal operations.
        if subcommand in {"branch", "tag", "switch", "checkout", "stash"} and (
            _GIT_DESTRUCTIVE_FLAGS & flags
        ):
            return CommandEffect.DESTRUCTIVE_MUTATION, (), "write", "none", "destructive"
        if subcommand in _LOCAL_GIT:
            return CommandEffect.REVERSIBLE_MUTATION, (), "write", "none", "reversible"
        # Unknown Git subcommand: treat as a high-impact mutation, never as a read.
        return CommandEffect.REVERSIBLE_MUTATION, (), "write", "none", "reversible"

    if executable in _PACKAGE_MANAGERS:
        args = [item.lower() for item in words[1:]]
        if not any(item in _PACKAGE_MUTATING_SUBCOMMANDS for item in args):
            return CommandEffect.READ_ONLY, (), "read", "none", "reversible"
        if _package_scope(args) == "global":
            return CommandEffect.PRIVILEGED_HOST_MUTATION, (), "write", "network", "destructive"
        return CommandEffect.REVERSIBLE_MUTATION, (), "write", "network", "reversible"

    if executable in _NETWORK_FETCH_EXECUTABLES:
        # A network *fetch* is not a mutation: it reads a remote resource.
        # It keeps the network effect so the engine's ALLOW_NETWORK rule owns it.
        return CommandEffect.READ_ONLY, (), "none", "network", "reversible"

    if executable in _READ_ONLY_EXECUTABLES:
        return CommandEffect.READ_ONLY, (), "read", "none", "reversible"

    if executable in _MUTATING_EXECUTABLES:
        return (
            CommandEffect.REVERSIBLE_MUTATION,
            _resolve(_positional_targets(raw_argv)),
            "write",
            "none",
            "reversible",
        )

    # Unknown executable: never assume safe.
    return CommandEffect.REVERSIBLE_MUTATION, (), "write", "none", "reversible"


class PermissionEngine:
    """Single authority for ALLOW / APPROVAL_REQUIRED / DENY.

    Canonical precedence, applied for every operation regardless of caller:

    1. scope escape                     -> DENY
    2. canonical command semantics      -> host / remote / destructive gate
    3. host, secret, protected-path and privilege boundaries
    4. read-only, project-Git, user-runtime, network and worker allows
    5. project/user mutation            -> ALLOW
    6. anything else                    -> APPROVAL_REQUIRED

    Steps 1-2 deliberately precede every ALLOW branch. A command whose effect
    cannot be classified, or whose target cannot be resolved, must never reach
    an allow by losing information.
    """

    def evaluate(self, context: OperationContext) -> PermissionDecision:
        scope = _scope(context)
        words = _command_words(context.command)
        executable = words[0] if words else ""
        operation = context.operation.lower()
        try:
            effect = CommandEffect(str(context.command_effect))
        except ValueError:
            effect = CommandEffect.NONE
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
        # Canonical command semantics. Scope and danger are orthogonal: a
        # project-scoped destructive command is still destructive, and an
        # unresolvable target must never soften the verdict. These branches sit
        # before every ALLOW path so no caller can reach one by losing a target.
        if effect is CommandEffect.PRIVILEGED_HOST_MUTATION:
            return PermissionDecision(
                Decision.APPROVAL_REQUIRED,
                ReasonCode.APPROVAL_HOST_PRIVILEGE,
                scope,
                effects,
                operation,
            )
        if effect is CommandEffect.REMOTE_IRREVERSIBLE_MUTATION:
            return PermissionDecision(
                Decision.APPROVAL_REQUIRED,
                ReasonCode.APPROVAL_IRREVERSIBLE_REMOTE,
                scope,
                effects,
                operation,
            )
        if effect is CommandEffect.REMOTE_MUTATION:
            return PermissionDecision(
                Decision.APPROVAL_REQUIRED,
                ReasonCode.APPROVAL_HOST_PRIVILEGE,
                scope,
                effects,
                operation,
            )
        if effect is CommandEffect.DESTRUCTIVE_MUTATION:
            # An unknown destructive target is high impact, not a user write.
            if not context.target_paths:
                return PermissionDecision(
                    Decision.APPROVAL_REQUIRED,
                    ReasonCode.APPROVAL_UNKNOWN_HIGH_IMPACT,
                    scope,
                    effects,
                    operation,
                )
            return PermissionDecision(
                Decision.APPROVAL_REQUIRED,
                ReasonCode.APPROVAL_HOST_DESTRUCTIVE,
                scope,
                effects,
                operation,
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
    """Build a normalized context for compatibility callers.

    Command semantics are classified once, here, and carried on the context as
    ``command_effect`` plus the resolved target paths.  Callers never re-derive
    meaning from argv, so every layer reaches the same decision.
    """

    argv = tuple(shlex.split(command) if isinstance(command, str) else command)
    executable = Path(argv[0]).name if argv else ""
    effect, target_paths, filesystem_effect, network_effect, reversibility = classify_command(
        argv, cwd=cwd
    )
    return OperationContext(
        tool="shell.exec",
        operation="shell.exec",
        workspace_root=workspace_root,
        cwd=cwd,
        command=argv,
        target_paths=target_paths,
        filesystem_effect=filesystem_effect,
        network_effect=network_effect,
        git_effect="command" if executable == "git" else "none",
        service_effect=(
            "user"
            if executable == "systemctl" and "--user" in argv
            else "system"
            if executable == "systemctl"
            else "none"
        ),
        # Privilege is decided from ``command_effect`` below, not asserted here:
        # pre-setting it would collapse scope and pre-empt the engine's own
        # sudo / service rules.
        privilege_level="user",
        reversibility=reversibility,
        remote_effect=(
            "mutation"
            if effect in {CommandEffect.REMOTE_MUTATION, CommandEffect.REMOTE_IRREVERSIBLE_MUTATION}
            else "none"
        ),
        command_effect=str(effect),
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
