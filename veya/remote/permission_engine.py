"""Canonical permission decision engine.

The engine classifies normalized effects and scopes.  Adapters may translate
their native request into :class:`OperationContext`, but they must not invent a
second approval policy.  This module deliberately contains no executor or
sandbox code: it only returns a decision and an auditable reason.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
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
    ALLOW_TRUSTED_ADMIN = "ALLOW_TRUSTED_ADMIN"
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


_HOST_ROOTS = tuple(
    Path(item)
    for item in ("/etc", "/usr", "/boot", "/dev", "/proc", "/sys", "/opt", "/var", "/root")
)


def _user_runtime_roots() -> tuple[Path, ...]:
    """Veya-owned user runtime/config roots, plus the canonical user systemd dir.

    Deliberately a short, explicit allowlist rather than ``$HOME``: ordinary
    Local2 self-development needs to manage its own runtime state, but that must
    not become a blanket allow for personal data.  ``~/.ssh``, ``~/.gnupg`` and
    ``~/.aws`` are absent by design and stay gated by the credential boundary.
    """
    home = Path.home().expanduser()
    return (
        home / ".config" / "systemd" / "user",
        home / ".config" / "veya",
        home / ".veya",
        home / ".local" / "share" / "veya",
        home / ".cache" / "veya",
    )


_DESTRUCTIVE_WORDS = frozenset({"mkfs", "fdisk", "parted", "mount", "umount", "iptables", "nft"})
# systemctl verbs that only inspect state.  Classifying them as read-only keeps
# `systemctl --user status|show` from being reported as a mutation.
# systemctl verbs that take the machine down.  Distinct from ordinary service
# administration, which is privileged but reversible.
# Commands whose operands name file content.  A ``..`` operand on one of these
# is an escape attempt; the same operand on version-control plumbing is not.
_TRAVERSAL_GATED_EXECUTABLES = frozenset(
    {
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "tee",
        "cp",
        "mv",
        "rm",
        "sed",
        "awk",
        "grep",
        "rg",
        "install",
        "chmod",
        "chown",
        "dd",
        "truncate",
        "tar",
        "mkdir",
        "touch",
        "ln",
        "patch",
        "shred",
        "vi",
        "vim",
        "nano",
        "emacs",
        "code",
    }
)
_SYSTEMCTL_POWER_VERBS = frozenset({"poweroff", "reboot", "halt", "kexec", "suspend", "hibernate"})
_SYSTEMCTL_READ_ONLY_VERBS = frozenset(
    {
        "status",
        "show",
        "is-active",
        "is-enabled",
        "is-failed",
        "list-units",
        "list-unit-files",
        "list-timers",
        "list-sockets",
        "cat",
        "get-default",
        "show-environment",
        "list-dependencies",
        "list-jobs",
    }
)
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


def _under(path: Path, roots: Iterable[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _escapes_workspace_by_dotdot(context: OperationContext) -> bool:
    """True when a ``..`` operand climbs out of the workspace root.

    ``DENY_PATH_TRAVERSAL`` existed as a reason code but nothing ever raised it,
    so ``cat ../../../../etc/passwd`` resolved to an ordinary read and was
    ALLOWed.  Only *escaping* traversal is denied: ``cat ../sibling.py`` from a
    subdirectory is normal work and stays allowed.
    """
    root = _canonical(context.workspace_root) if context.workspace_root else None
    if root is None:
        return False
    # Only content access is gated.  VCS plumbing legitimately places new
    # objects relative to the repository (`git worktree add ../probe`), which
    # locked policy treats as ordinary project mutation; that must not become a
    # second, divergent escape policy.
    words = _command_words(context.command)
    if not words or words[0] not in _TRAVERSAL_GATED_EXECUTABLES:
        return False
    cwd = _canonical(context.cwd) if context.cwd else root
    for operand in _positional_targets(context.command or ()):
        if ".." not in Path(operand).parts:
            continue
        try:
            resolved = Path(os.path.normpath(str(cwd / operand)))
        except (OSError, ValueError):
            return True
        if resolved != root and root not in resolved.parents:
            return True
    return False


def _scope(context: OperationContext) -> Scope:
    root = _canonical(context.workspace_root) if context.workspace_root else None
    targets = tuple(_canonical(path) for path in context.target_paths)
    words = _command_words(context.command)
    executable = words[0] if words else ""

    # Host credential stores are classified as HOST but are additionally marked
    # so the credential boundary can keep gating them; scope alone never allows.
    if any(_under(path, _HOST_ROOTS) for path in targets):
        return Scope.HOST
    # User-owned Veya runtime state (including ~/.config/systemd/user) is USER
    # scope, not a project escape.  Local2 administering its own service is
    # ordinary operation, and it used to be rejected as DENY_SCOPE_ESCAPE.
    user_roots = _user_runtime_roots()
    if any(_under(path, user_roots) for path in targets):
        return Scope.USER
    if root and targets:
        if all(path == root or root in path.parents for path in targets):
            return Scope.PROJECT
        return Scope.OUTSIDE_ALLOWED_SCOPE
    # No resolvable target: classify by the operation's own semantics rather
    # than falling through to "escape".  `sudo systemctl daemon-reload` has no
    # path operand at all, and must read as HOST admin, not a scope escape.
    if context.service_effect == "system" or context.privilege_level in {"root", "host"}:
        return Scope.HOST
    if executable in {"sudo", "doas"} or (executable == "systemctl" and "--user" not in words):
        return Scope.HOST
    if context.remote_effect != "none" or context.network_effect != "none":
        # Must precede the user-runtime branch: `git push` has no path operand,
        # and a greedy privilege_level fallback was labelling it USER scope.
        return Scope.REMOTE
    if context.service_effect == "user":
        return Scope.USER
    if context.filesystem_effect in {"read", "none"} and context.process_effect in {
        "none",
        "inspect",
    }:
        # A pure inspection with no path operand is not an escape.
        return Scope.USER if executable == "sudo" else Scope.PROJECT
    if context.privilege_level == "user":
        # Ordinary unprivileged operation with no resolvable path operand:
        # local Git plumbing and the like.  Checked last so it cannot outrank
        # the remote or service branches above.
        return Scope.USER
    return Scope.OUTSIDE_ALLOWED_SCOPE


# Credential/secret files whose *contents* must not flow into a session
# unreviewed. This is step 3 of the canonical precedence ("secret,
# protected-path boundaries") for the read path: extraction happens in the
# parser for every read, but only these names gate. Everything else readable
# (passwd, hostname, proc, project files) keeps its existing verdict.
_SENSITIVE_READ_BASENAMES = frozenset({"shadow", "gshadow", "sudoers", "credentials"})
_SENSITIVE_READ_SUFFIXES = (".pem",)


def _is_sensitive_read_target(path: Path) -> bool:
    name = path.name
    if name in _SENSITIVE_READ_BASENAMES:
        return True
    if name.endswith(_SENSITIVE_READ_SUFFIXES):
        return True
    # SSH private keys (never the .pub halves).
    if name.startswith("id_") and not name.endswith(".pub") and ".ssh" in path.parts:
        return True
    # /etc/sudoers.d/* drop-ins.
    parts = path.parts
    return len(parts) >= 4 and parts[1] == "etc" and parts[2] == "sudoers.d"


# read-only command family. Anything else starting with `-` is a boolean flag.
_READ_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "head": frozenset({"-n", "-c", "--lines", "--bytes"}),
    "tail": frozenset({"-n", "-c", "--lines", "--bytes", "--pid"}),
    "ls": frozenset(
        {"-I", "--ignore", "--color", "--time-style", "--sort", "--format", "--width", "--tabsize"}
    ),
    "stat": frozenset({"-c", "--printf"}),
    "grep": frozenset(
        {
            "-m",
            "-A",
            "-B",
            "-C",
            "--max-count",
            "--after-context",
            "--before-context",
            "--context",
            "--color",
            "--include",
            "--exclude",
            "--exclude-dir",
            "--label",
        }
    ),
    "egrep": frozenset(
        {"-m", "-A", "-B", "-C", "--max-count", "--color", "--include", "--exclude"}
    ),
    "fgrep": frozenset(
        {"-m", "-A", "-B", "-C", "--max-count", "--color", "--include", "--exclude"}
    ),
    "rg": frozenset(
        {"-m", "-A", "-B", "-C", "--max-count", "--colors", "--glob", "--iglob", "--type", "-t"}
    ),
}
# grep-family: -e takes a search PATTERN (skip), -f takes a FILE (keep as path).
_READ_PATTERN_FLAGS = frozenset({"-e", "--regexp"})
_READ_FILE_FLAGS = frozenset({"-f", "--file"})
# Read-only executables whose positional operands are file paths. Every other
# read-only executable (echo, pwd, python, pytest, curl, …) keeps `()` targets:
# their positionals are text, code, or URLs, not workspace paths.
_READ_PATH_EXECUTABLES = frozenset(
    {"cat", "head", "tail", "ls", "wc", "stat", "file", "grep", "egrep", "fgrep", "rg"}
)
_GREP_FAMILY = frozenset({"grep", "egrep", "fgrep", "rg"})


def _read_targets(executable: str, raw_argv: Sequence[str]) -> tuple[str, ...]:
    """Positional path operands of a read-only command, case-preserved.

    Only for executables whose positionals are file paths. Flags, flag values,
    and (for the grep family) the search pattern are excluded — a pattern must
    never be mistaken for a target, and a flag value like `5` in
    `head -n 5 file` must never resolve into the workspace. Returns raw strings
    for the caller to resolve; makes no policy decision.
    """
    if executable not in _READ_PATH_EXECUTABLES:
        return ()
    args = [str(a) for a in raw_argv[1:]]
    paths: list[str] = []
    pattern_seen = False
    value_flags = _READ_VALUE_FLAGS.get(executable, frozenset())
    i = 0
    while i < len(args):
        tok = args[i]
        low = tok.lower()
        if tok == "--":
            paths.extend(args[i + 1 :])
            break
        if tok.startswith("-") and tok != "-":
            if low in _READ_PATTERN_FLAGS:
                i += 2  # -e PATTERN: a pattern, not a path
                pattern_seen = True
                continue
            if low in _READ_FILE_FLAGS:
                if i + 1 < len(args):
                    paths.append(args[i + 1])  # -f FILE: a genuine path
                i += 2
                pattern_seen = True
                continue
            if low in value_flags:
                i += 2  # --flag VALUE: a setting, not a path
                continue
            i += 1  # boolean flag (incl. -n5 / -efoo attached forms)
            continue
        if executable in _GREP_FAMILY and not pattern_seen:
            pattern_seen = True  # first positional is the search pattern
        else:
            paths.append(tok)
        i += 1
    return tuple(paths)


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
        # Power state.  These were unclassified, so they fell through to the
        # unknown-executable branch and resolved to a *reversible* project
        # mutation -- a bare `reboot` or `shutdown -h now` was ALLOWed with no
        # approval at all.  Taking the machine down is not a project edit.
        "reboot",
        "poweroff",
        "halt",
        "shutdown",
        "kexec",
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
        # ``tee`` was unclassified and fell through to the unknown-executable
        # branch, which returns no target paths.  With no targets the scope
        # could not be resolved, so ``tee /etc/systemd/system/x.service``
        # resolved to USER scope and was ALLOWed -- a pre-existing hole where a
        # host write looked like an ordinary project mutation.
        "tee",
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


# Shell composition.  A command line can carry more than one program and more
# than one write target, and a classifier that only reads the first token sees
# none of it: ``printf x | tee /etc/veya.conf`` classified as an unknown
# `printf` with no targets, and ``echo hi > /etc/veya.conf`` never mentioned the
# redirect at all.  Both were ALLOW.  So a composed line is split and every
# segment, plus every redirection target, is classified and then merged with the
# most restrictive result winning.
_SHELL_CONTROL_TOKENS = frozenset({"|", "||", "&&", ";", "&", "\n"})
_REDIRECT_TOKENS = (">>", ">&", "<&", ">|", ">", "<")
_REDIRECT_FD_PREFIX = re.compile(r"^\d*>")

# Most restrictive first.  Merging walks this order and keeps the first hit.
_EFFECT_SEVERITY = (
    CommandEffect.DESTRUCTIVE_MUTATION,
    CommandEffect.PRIVILEGED_HOST_MUTATION,
    CommandEffect.REMOTE_IRREVERSIBLE_MUTATION,
    CommandEffect.REMOTE_MUTATION,
    CommandEffect.REVERSIBLE_MUTATION,
    CommandEffect.READ_ONLY,
    CommandEffect.NONE,
)


def _split_composed(argv: Sequence[str]) -> tuple[list[list[str]], list[str]]:
    """Split a command line into program segments plus redirection targets."""
    segments: list[list[str]] = [[]]
    redirects: list[str] = []
    # A redirect operator either carries its target inline (`>/etc/x`) or takes
    # the next token as the target (`> /etc/x`).  Both forms must record a
    # target, or `echo hi > /etc/x` silently loses the only path it touches.
    pending_redirect = False
    for token in (str(item) for item in argv):
        if token in _SHELL_CONTROL_TOKENS:
            if segments[-1]:
                segments.append([])
            pending_redirect = False
            continue
        if pending_redirect:
            redirects.append(token)
            pending_redirect = False
            continue
        stripped = _REDIRECT_FD_PREFIX.sub(">", token)
        matched = next((op for op in _REDIRECT_TOKENS if stripped.startswith(op)), None)
        if matched is not None:
            remainder = stripped[len(matched) :].strip()
            if remainder:
                redirects.append(remainder)
            else:
                pending_redirect = True
            continue
        segments[-1].append(token)
    return [segment for segment in segments if segment], redirects


def _merge_effects(
    parts: Sequence[tuple[CommandEffect, tuple[Path, ...], str, str, str]],
) -> tuple[CommandEffect, tuple[Path, ...], str, str, str]:
    """Combine per-segment results; the most restrictive classification wins."""
    if not parts:
        return CommandEffect.NONE, (), "none", "none", "reversible"
    targets: list[Path] = []
    for _effect, part_targets, _fs, _net, _rev in parts:
        targets.extend(part_targets)
    unique: list[Path] = []
    for target in targets:
        if target not in unique:
            unique.append(target)
    effect = next(
        (
            candidate
            for candidate in _EFFECT_SEVERITY
            if any(part[0] is candidate for part in parts)
        ),
        CommandEffect.NONE,
    )
    filesystem = (
        "write"
        if any(part[2] == "write" for part in parts)
        else "read"
        if any(part[2] == "read" for part in parts)
        else "none"
    )
    network = "network" if any(part[3] == "network" for part in parts) else "none"
    reversibility = (
        "destructive"
        if any(part[4] == "destructive" for part in parts)
        else "irreversible"
        if any(part[4] == "irreversible" for part in parts)
        else "reversible"
    )
    return effect, tuple(unique), filesystem, network, reversibility


def classify_command(
    argv: Sequence[str], *, cwd: Path
) -> tuple[CommandEffect, tuple[Path, ...], str, str, str]:
    """Describe one command line.

    Returns ``(effect, target_paths, filesystem_effect, network_effect,
    reversibility)``.  Classification is argument-aware: ``rm file`` and
    ``rm -rf /`` are both destructive but resolve to different targets, and
    ``git push`` differs from ``git push --force``.
    """
    raw_argv = [str(item) for item in argv]
    segments, redirects = _split_composed(raw_argv)
    if len(segments) > 1 or redirects:
        # Every segment and every redirect target is classified, then merged.
        # The merge is what stops a pipe or a `>` from hiding a host write.
        parts = [_classify_single(segment, cwd=cwd) for segment in segments]
        for redirect in redirects:
            candidate = Path(redirect).expanduser()
            parts.append(
                (
                    CommandEffect.REVERSIBLE_MUTATION,
                    (candidate if candidate.is_absolute() else cwd / candidate,),
                    "write",
                    "none",
                    "reversible",
                )
            )
        return _merge_effects(parts)
    return _classify_single(raw_argv, cwd=cwd)


def _classify_single(
    argv: Sequence[str], *, cwd: Path
) -> tuple[CommandEffect, tuple[Path, ...], str, str, str]:
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
        # Canonical rule: the sudo prefix dominates. Running anything as root is
        # a privileged host operation, and the subcommand's semantics must never
        # downgrade that privilege — not to reversible, not to destructive, not
        # to remote. One predictable verdict for every sudo invocation.
        if not raw_argv[1:]:
            # Bare `sudo`: an interactive root shell.
            return (
                CommandEffect.PRIVILEGED_HOST_MUTATION,
                (),
                "write",
                "none",
                "irreversible",
            )
        inner = _command_words(raw_argv[1:])
        _inner_effect, _inner_targets, _inner_fs, _inner_net, _inner_rev = classify_command(
            raw_argv[1:], cwd=cwd
        )
        # The sudo prefix dominates: one predictable effect for every sudo
        # invocation (locked by tests/remote/test_command_semantics_matrix.py).
        # Destructiveness is carried by ``reversibility`` instead of by the
        # effect, so `sudo rm -rf /` stays a high-risk gate while
        # `sudo systemctl restart` stays ordinary reversible host admin, and
        # trusted-admin mode can tell them apart without a second authority.
        return (
            CommandEffect.PRIVILEGED_HOST_MUTATION,
            (),
            "write",
            "network" if inner and inner[0] in _NETWORK_FETCH_EXECUTABLES else "none",
            "destructive"
            if _inner_effect is CommandEffect.DESTRUCTIVE_MUTATION
            else ("irreversible" if _inner_effect is CommandEffect.NONE else _inner_rev),
        )

    if executable == "systemctl":
        user_scoped = "--user" in words
        if user_scoped:
            # `systemctl --user` manages the caller's own service manager
            # instance.  That is USER scope regardless of the executable being
            # systemctl, so it is never treated as privileged host mutation.
            # Inspection verbs are read-only; the rest are reversible mutation
            # and still resolve through the user-runtime path.
            subcommand = next((word for word in words[1:] if not word.startswith("-")), "")
            if subcommand in _SYSTEMCTL_READ_ONLY_VERBS:
                return CommandEffect.READ_ONLY, (), "read", "none", "reversible"
            return CommandEffect.REVERSIBLE_MUTATION, (), "none", "none", "reversible"
        subcommand = next((word for word in words[1:] if not word.startswith("-")), "")
        if subcommand in _SYSTEMCTL_READ_ONLY_VERBS:
            return CommandEffect.READ_ONLY, (), "read", "none", "reversible"
        if subcommand in _SYSTEMCTL_POWER_VERBS:
            return CommandEffect.DESTRUCTIVE_MUTATION, (), "write", "none", "destructive"
        # Ordinary host service administration: privileged, but reversible.
        return CommandEffect.PRIVILEGED_HOST_MUTATION, (), "write", "none", "reversible"

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
        # Read-only keeps its effect, but the path semantics must not be lost:
        # positional file operands are extracted and normalized so the existing
        # workspace scope authority can see an escape. The verdict itself is
        # unchanged — the engine, not this parser, decides ALLOW vs DENY.
        return (
            CommandEffect.READ_ONLY,
            _resolve(_read_targets(executable, raw_argv)),
            "read",
            "none",
            "reversible",
        )

    if executable == "sed":
        # In-place sed rewrites its operands; without -i it only reads.
        in_place = any(flag in words for flag in ("-i", "--in-place"))
        return (
            CommandEffect.REVERSIBLE_MUTATION if in_place else CommandEffect.READ_ONLY,
            _resolve(_positional_targets(raw_argv)),
            "write" if in_place else "read",
            "none",
            "reversible",
        )

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


# One canonical trusted-admin policy.  Host administration is executable by
# default (APPROVAL_REQUIRED, not DENY); an operator who has already decided
# that this Local2 instance may administer the machine sets this explicitly.
# It is intentionally a single, explicit value rather than scattered
# command-specific bypasses, and it never downgrades a destructive, credential,
# remote-irreversible, or any DENY outcome.
TRUSTED_ADMIN_ENV = "LOCAL2_ADMIN_TRUST"
_TRUSTED_ADMIN_ENABLED = "enabled"

# Reason codes that trusted-admin mode must never soften.  A machine-wide
# "I trust this agent with the host" does not mean "erase the disk" or "print
# /etc/shadow".
_TRUSTED_ADMIN_NEVER_DOWNGRADES = frozenset(
    {
        ReasonCode.APPROVAL_HOST_DESTRUCTIVE,
        ReasonCode.APPROVAL_UNKNOWN_HIGH_IMPACT,
        ReasonCode.APPROVAL_SECURITY_BOUNDARY,
        ReasonCode.APPROVAL_IRREVERSIBLE_REMOTE,
        ReasonCode.DENY_SCOPE_ESCAPE,
        ReasonCode.DENY_INVALID_APPROVAL,
        ReasonCode.DENY_AUTHORITY_VIOLATION,
        ReasonCode.DENY_PATH_TRAVERSAL,
    }
)


def trusted_admin_enabled() -> bool:
    """True only when the operator opted in with the exact sentinel value."""
    return (os.environ.get(TRUSTED_ADMIN_ENV) or "").strip().lower() == _TRUSTED_ADMIN_ENABLED


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
        """Single authority. Applies the one trusted-admin policy at one place."""
        return self._apply_trusted_admin(context, self._evaluate(context))

    def _apply_trusted_admin(
        self, context: OperationContext, decision: PermissionDecision
    ) -> PermissionDecision:
        """The one place host administration may be auto-approved.

        Two independent guards, because a reason code alone is not enough:
        ``sudo rm -rf /`` classifies as PRIVILEGED_HOST_MUTATION, whose reason
        is otherwise upgradable.  Trusted administration of the host is not
        consent to irreversible destruction, so reversibility is checked
        separately as well.
        """
        if not trusted_admin_enabled():
            return decision
        if decision.decision is not Decision.APPROVAL_REQUIRED:
            return decision
        if decision.reason in _TRUSTED_ADMIN_NEVER_DOWNGRADES:
            return decision
        try:
            effect = CommandEffect(str(context.command_effect))
        except ValueError:
            return decision
        if effect is CommandEffect.DESTRUCTIVE_MUTATION:
            return decision
        if context.reversibility == "destructive":
            return decision
        if decision.scope not in {Scope.HOST, Scope.USER, Scope.REMOTE}:
            return decision
        return replace(decision, decision=Decision.ALLOW, reason=ReasonCode.ALLOW_TRUSTED_ADMIN)

    def _evaluate(self, context: OperationContext) -> PermissionDecision:
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

        if _escapes_workspace_by_dotdot(context):
            return PermissionDecision(
                Decision.DENY, ReasonCode.DENY_PATH_TRAVERSAL, scope, effects, operation
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
        if context.filesystem_effect == "read" and any(
            _is_sensitive_read_target(_canonical(path)) for path in context.target_paths
        ):
            # Step 3 precedes the read-only ALLOW: credential contents gate.
            return PermissionDecision(
                Decision.APPROVAL_REQUIRED,
                ReasonCode.APPROVAL_SECURITY_BOUNDARY,
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


#: Interpreters and trampolines that defer the real command to a nested string.
#: classify_command grades on argv[0], so a wrapper is graded as itself and the
#: command it hides is never seen. Measured 2026-10-04: bare `git push origin
#: main` is REMOTE_MUTATION and requires approval, while `sh -c 'git push origin
#: main'` was graded REVERSIBLE_MUTATION and allowed outright.
#: Worst-wins ordering for wrapper unwrapping. Lower is milder.
_SEVERITY: dict[CommandEffect, int] = {
    CommandEffect.NONE: 0,
    CommandEffect.READ_ONLY: 1,
    CommandEffect.REVERSIBLE_MUTATION: 2,
    CommandEffect.REMOTE_MUTATION: 3,
    CommandEffect.REMOTE_IRREVERSIBLE_MUTATION: 4,
    CommandEffect.DESTRUCTIVE_MUTATION: 5,
    CommandEffect.PRIVILEGED_HOST_MUTATION: 6,
}

_COMMAND_WRAPPERS = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "csh",
        "tcsh",
        "fish",
        "ssh",
        "scp",
        "sftp",
        "nohup",
        "timeout",
        "watch",
        "xargs",
        "eval",
        "env",
    }
)


def unmodelled_command_construct(command: str) -> str | None:
    """Name a construct this parser cannot honestly classify, or None.

    `shlex.split` runs before classification, so by the time argv exists the
    structure that mattered is gone: `;` and a newline are ordinary tokens, and
    `sh -c 'git push'` is just the executable `sh`. Rather than re-parse quoting
    — which is easy to get subtly wrong and fails open when it is — this names
    the construct and lets the caller fail safe.

    The wrappers are listed rather than unwrapped on purpose. Unwrapping needs
    correct handling of nested quotes, and a mistake there silently downgrades a
    dangerous command, which is the exact failure being fixed.
    """
    text = str(command or "")
    if not text.strip():
        return None
    if "$(" in text or "`" in text:
        return "command_substitution"
    if "\n" in text or "\r" in text:
        return "newline_separator"
    if ";" in text:
        # Only ";" is unmodelled. Pipes and &&/||/& are already classified:
        # `ls | grep foo` is a read-only composition and must stay allowed, and
        # `echo ok | touch pwn` is already REVERSIBLE_MUTATION. Escalating them
        # here broke ordinary reads, which unit-fast did not catch.
        return "semicolon_separator"
    try:
        first = shlex.split(text)[0]
    except ValueError:
        # Unbalanced quoting is itself unmodellable.
        return "unbalanced_quoting"
    if Path(first).name in _COMMAND_WRAPPERS:
        inner = _wrapper_inner_command(text, Path(first).name)
        return f"wrapper:{Path(first).name}" if inner is None else None
    return None


def _wrapper_inner_command(text: str, wrapper: str) -> str | None:
    """The command a wrapper defers to, or None when it cannot be recovered.

    The engine already unwraps `sh -c` / `bash -lc` for a hardcoded denylist of
    privileged binaries, which is why `bash -lc 'sudo apt-get install curl'` was
    caught while `bash -lc 'git push'` was not: `git` is simply absent from that
    list. Re-classifying the inner command is strictly better than extending the
    list, and strictly better than escalating every wrapper, because it keeps
    `bash -lc 'pytest && ruff check .'` allowed while catching what it hides.
    """
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    for index, token in enumerate(tokens):
        if token in {"-c", "-lc", "-cl", "-lic", "-ilc"}:
            inner = tokens[index + 1 :]
            return " ".join(inner) if inner else None
    if wrapper in {"ssh", "scp", "sftp"}:
        # host is argv[1]; the remote command is the remainder.
        return " ".join(tokens[2:]) or None
    return None


def parse_command_context(
    command: str | Iterable[str], *, cwd: Path, workspace_root: Path
) -> OperationContext:
    """Build a normalized context for compatibility callers.

    Command semantics are classified once, here, and carried on the context as
    ``command_effect`` plus the resolved target paths.  Callers never re-derive
    meaning from argv, so every layer reaches the same decision.
    """

    unmodelled = unmodelled_command_construct(command) if isinstance(command, str) else None
    argv = tuple(shlex.split(command) if isinstance(command, str) else command)
    executable = Path(argv[0]).name if argv else ""
    effect, target_paths, filesystem_effect, network_effect, reversibility = classify_command(
        argv, cwd=cwd
    )
    if unmodelled is not None:
        # Fail safe. We cannot see what is inside, so we assume the worst class
        # rather than guess a lower one: an under-estimate here is the bypass this
        # closes. PRIVILEGED_HOST_MUTATION cannot be ALLOWed by any later branch,
        # and it is not downgraded even by trusted-admin, which excludes it by name.
        effect = CommandEffect.PRIVILEGED_HOST_MUTATION
        reversibility = "destructive"
    else:
        # A wrapper's inner command is classified in its own right and the worse of
        # the two wins, so hiding a mutation behind `sh -c` cannot lower the verdict
        # while a genuinely safe wrapper still stays allowed.
        # Only when the raw string survives; an argv passed in by a caller has
        # already been through the caller's own tokenizer.
        inner = _wrapper_inner_command(command, executable) if isinstance(command, str) else None
        if inner:
            inner_effect = classify_command(shlex.split(inner), cwd=cwd)[0]
            if inner_effect is CommandEffect.PRIVILEGED_HOST_MUTATION:
                effect = CommandEffect.PRIVILEGED_HOST_MUTATION
                reversibility = "destructive"
            elif _SEVERITY.get(inner_effect, 0) > _SEVERITY.get(effect, 0):
                effect = inner_effect
                if inner_effect in {
                    CommandEffect.REMOTE_MUTATION,
                    CommandEffect.REMOTE_IRREVERSIBLE_MUTATION,
                }:
                    network_effect = "network"
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
