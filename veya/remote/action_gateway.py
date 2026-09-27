"""ActionGateway and classification engine (spec §1-§39).

Enforces:
1. SAFE_CAPABILITIES_FULLY_OPEN (AUTO_OPEN):
   Workspace files, normal git, direct argv commands, python/node inline execution,
   user systemd on managed units, read-only diagnostics/logs, and managed home roots.
   No per-call approved flag required.
2. PRIVILEGED_CAPABILITIES_HUMAN_GATED (HUMAN_GATED):
   Shell wrappers (bash -lc/sh -c), sudo, root shell, system service mutations,
   destructive git, privileged docker, host package managers, network admin, storage admin.
   Rejects boolean approved=true (INVALID_APPROVAL); strictly enforces server-issued approval_id.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .approval import ApprovalStore, compute_operation_hash, get_approval_store
from .executor_registry import get_executor_registry
from .models import ManagedUserService, RemoteErrorCode, RemoteSession, RiskClass
from .permission_engine import (
    Decision,
    OperationContext,
    PermissionEngine,
    ReasonCode,
    parse_command_context,
)
from .workspace_policy import classify_destructive


class ActionCategory(StrEnum):
    AUTO_OPEN = "AUTO_OPEN"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"
    # Compatibility name for older callers.  It is an alias, not a second
    # policy state.
    HUMAN_GATED = "REQUIRE_APPROVAL"


class ActionClassification:
    def __init__(
        self,
        category: ActionCategory,
        capability_id: str,
        *,
        risk_class: RiskClass | None = None,
        normalized_operation: str = "",
        operation_hash: str = "",
        target: str = "",
        reason: str = "",
    ) -> None:
        self.category = category
        self.capability_id = capability_id
        self.risk_class = risk_class
        self.normalized_operation = normalized_operation
        self.operation_hash = operation_hash
        self.target = target
        self.reason = reason

    @property
    def requires_approval(self) -> bool:
        return self.category == ActionCategory.REQUIRE_APPROVAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": str(self.category),
            "capability_id": self.capability_id,
            "risk_class": str(self.risk_class) if self.risk_class else None,
            "normalized_operation": self.normalized_operation,
            "operation_hash": self.operation_hash,
            "target": self.target,
            "reason": self.reason,
            "requires_approval": self.requires_approval,
        }


@dataclass(frozen=True)
class SystemctlInvocation:
    """Normalized systemctl argv shared by policy and execution layers."""

    argv: tuple[str, ...]
    scope: str
    action: str
    unit: str | None
    options: tuple[str, ...]


_SYSTEMCTL_READ_ACTIONS = frozenset(
    {
        "cat",
        "show",
        "status",
        "is-active",
        "is-enabled",
        "list-units",
        "list-unit-files",
        "show-environment",
    }
)
_SYSTEMCTL_USER_MUTATIONS = frozenset(
    {"start", "stop", "restart", "reload", "try-restart", "enable", "disable"}
)
_SYSTEMCTL_READ_FLAGS = frozenset({"--value", "--all", "--full", "--no-pager"})
_SYSTEMCTL_READ_VALUE_FLAGS = frozenset({"-p", "--property", "--type", "--state"})


def parse_systemctl_argv(argv: Sequence[str]) -> SystemctlInvocation | None:
    """Parse systemctl's flexible global-option and action placement once."""

    values = tuple(str(item) for item in argv)
    if not values or Path(values[0]).name != "systemctl":
        return None
    scope = "system"
    options: list[str] = []
    action_index: int | None = None
    i = 1
    while i < len(values):
        token = values[i]
        if token == "--user":
            scope = "user"
            i += 1
            continue
        if token == "--system":
            scope = "system"
            i += 1
            continue
        if token in _SYSTEMCTL_READ_VALUE_FLAGS:
            if i + 1 >= len(values):
                return None
            options.append(token)
            i += 2
            continue
        if any(token.startswith(f"{flag}=") for flag in _SYSTEMCTL_READ_VALUE_FLAGS):
            options.append(token)
            i += 1
            continue
        if token in _SYSTEMCTL_READ_FLAGS or token.startswith("-"):
            options.append(token)
            i += 1
            continue
        action_index = i
        break
    if action_index is None:
        return None

    action = values[action_index].lower()
    unit: str | None = None
    i = action_index + 1
    while i < len(values):
        token = values[i]
        if (
            token in {";", "&&", "||", "|", "&", ">", "<", ">>"}
            or token.startswith("$(")
            or token.startswith("`")
        ):
            return None
        if token == "--user":
            scope = "user"
            i += 1
            continue
        if token == "--system":
            scope = "system"
            i += 1
            continue
        if token in _SYSTEMCTL_READ_VALUE_FLAGS:
            if i + 1 >= len(values):
                return None
            options.append(token)
            i += 2
            continue
        if any(token.startswith(f"{flag}=") for flag in _SYSTEMCTL_READ_VALUE_FLAGS):
            options.append(token)
            i += 1
            continue
        if token in _SYSTEMCTL_READ_FLAGS or token.startswith("-"):
            options.append(token)
            i += 1
            continue
        if unit is None:
            unit = token
        else:
            return None
        i += 1
    return SystemctlInvocation(values, scope, action, unit, tuple(options))


def parse_systemctl_command(command: str) -> SystemctlInvocation | None:
    try:
        return parse_systemctl_argv(shlex.split(command, posix=True))
    except ValueError:
        return None


def systemctl_read_options_valid(invocation: SystemctlInvocation) -> bool:
    for option in invocation.options:
        base = option.split("=", 1)[0]
        if base not in _SYSTEMCTL_READ_FLAGS and base not in _SYSTEMCTL_READ_VALUE_FLAGS:
            return False
    return True


class ManagedUserServiceRegistry:
    """Canonical registry of managed user-level systemd services (spec §10)."""

    MANAGED_PREFIXES = ("veya-", "hevi-", "stratum-")
    KNOWN_UNITS = frozenset({"veya-remote-mcp.service", "veya-openai-tunnel.service"})

    def __init__(self, user_config_dir: Path | None = None) -> None:
        self.user_config_dir = (
            user_config_dir or (Path.home() / ".config" / "systemd" / "user")
        ).resolve()

    def is_managed_unit(self, unit: str) -> bool:
        if not unit:
            return False
        clean = unit.strip()
        # Wildcards are strictly disallowed
        if any(char in clean for char in ("*", "?", "[", "]")):
            return False
        # Unit must strictly end with .service (no automatic appending)
        if not clean.endswith(".service"):
            return False
        name_part = clean[:-8]
        if not name_part or not all(c.isalnum() or c in "-_.@" for c in name_part):
            return False

        # Explicit known units
        if clean in self.KNOWN_UNITS:
            return True

        # Well-known project prefixes
        if any(clean.startswith(prefix) for prefix in self.MANAGED_PREFIXES):
            return True

        # Unit file exists in user's systemd config
        try:
            candidate = self.user_config_dir / clean
            if candidate.is_file() or candidate.is_symlink():
                return True
        except OSError:
            pass

        return False

    def list_managed_services(self) -> list[ManagedUserService]:
        results: list[ManagedUserService] = []
        for unit in sorted(self.KNOWN_UNITS):
            p = str(self.user_config_dir / unit)
            results.append(
                ManagedUserService(
                    unit=unit,
                    unit_path=p,
                    owner_project="veya",
                    discovered_at=0.0,
                )
            )
        if self.user_config_dir.is_dir():
            try:
                for entry in self.user_config_dir.glob("*.service"):
                    if entry.name not in self.KNOWN_UNITS:
                        results.append(
                            ManagedUserService(
                                unit=entry.name,
                                unit_path=str(entry),
                                owner_project="user",
                                discovered_at=0.0,
                            )
                        )
            except OSError:
                pass
        return results


_GLOBAL_SERVICE_REGISTRY = ManagedUserServiceRegistry()

# Critical system services where shutdown/restart poses host-level outage risk (spec §34)
_CRITICAL_SERVICES = frozenset(
    {
        "ssh.service",
        "sshd.service",
        "networkmanager.service",
        "systemd-networkd.service",
        "docker.service",
        "containerd.service",
        "display-manager.service",
        "systemd-logind.service",
        "gdm.service",
        "lightdm.service",
    }
)


def _parse_argv(command: str, *, shell_syntax: bool = False) -> list[str]:
    try:
        if shell_syntax:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
            lexer.whitespace_split = True
            lexer.commenters = ""
            return list(lexer)
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _shell_segments(command: str) -> list[str] | None:
    """Extract simple shell command segments without executing shell syntax.

    The gateway deliberately understands only command boundaries.  Expansion,
    redirection and subshell syntax are treated as approval-worthy because they
    cannot be safely reduced to an argv policy without a shell parser.
    """
    tokens = _parse_argv(command, shell_syntax=True)
    if not tokens:
        return None
    if any(token in {"<", ">", ">>", "<<", "(", ")"} for token in tokens):
        return None
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in {";", "&&", "||", "|", "&"}:
            if not segments[-1]:
                return None
            segments.append([])
        else:
            segments[-1].append(token)
    if not segments[-1]:
        return None
    return [" ".join(shlex.quote(part) for part in segment) for segment in segments]


def classify_action(
    tool_name: str,
    args: dict[str, Any],
    session: RemoteSession,
    cwd: str,
    *,
    service_registry: ManagedUserServiceRegistry | None = None,
) -> ActionClassification:
    """Classify any remote tool call into AUTO_OPEN or HUMAN_GATED (spec §1-§36)."""
    registry = service_registry or _GLOBAL_SERVICE_REGISTRY

    # 1. Non-shell tool calls
    if tool_name in ("file.read", "file.search", "artifact.list", "artifact.read"):
        norm_op = f"{tool_name} {args.get('path', '')}".strip()
        op_hash = compute_operation_hash(norm_op, cwd, "workspace.file_read")
        return ActionClassification(
            ActionCategory.AUTO_OPEN,
            "workspace.file_read",
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=str(args.get("path", "")),
            reason="safe workspace file read",
        )

    if tool_name in ("file.write", "file.patch", "artifact.write"):
        norm_op = f"{tool_name} {args.get('path', '')}".strip()
        op_hash = compute_operation_hash(norm_op, cwd, "workspace.file_write")
        if Path(str(args.get("path") or "")).name == ".env":
            return ActionClassification(
                ActionCategory.REQUIRE_APPROVAL,
                "secret.config_write",
                risk_class=RiskClass.P2_ROOT_MUTATION,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target=str(args.get("path", "")),
                reason="project .env mutation requires explicit approval",
            )
        return ActionClassification(
            ActionCategory.AUTO_OPEN,
            "workspace.file_write",
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=str(args.get("path", "")),
            reason="safe workspace file write within bound root",
        )

    if tool_name in ("workspace.list", "workspace.info"):
        op_hash = compute_operation_hash(tool_name, cwd, "workspace.info")
        return ActionClassification(
            ActionCategory.AUTO_OPEN,
            "workspace.info",
            normalized_operation=tool_name,
            operation_hash=op_hash,
            reason="workspace metadata inspection",
        )

    if tool_name in ("process.status", "process.cancel", "process.signal"):
        norm_op = f"{tool_name} {args.get('execution_id', '')}".strip()
        op_hash = compute_operation_hash(norm_op, cwd, "process.control")
        return ActionClassification(
            ActionCategory.AUTO_OPEN,
            "process.control",
            normalized_operation=norm_op,
            operation_hash=op_hash,
            reason="process control for owned execution",
        )

    if tool_name.startswith("autonomous.") or tool_name in ("interrupt.reply", "mission.revise"):
        op_hash = compute_operation_hash(tool_name, cwd, "autonomous.mission")
        return ActionClassification(
            ActionCategory.AUTO_OPEN,
            "autonomous.mission",
            normalized_operation=tool_name,
            operation_hash=op_hash,
            reason="canonical autonomous mission introspection and control",
        )

    # 2. Shell tool calls
    raw_command = str(args.get("command", "")).strip()
    argv = _parse_argv(raw_command)
    if not argv:
        op_hash = compute_operation_hash("empty", cwd, "shell.empty")
        return ActionClassification(
            ActionCategory.AUTO_OPEN,
            "shell.empty",
            normalized_operation="",
            operation_hash=op_hash,
        )

    norm_op = " ".join(argv)
    executable = Path(argv[0]).name.lower()
    lower_argv = [a.lower() for a in argv]

    # Rule 6: wrappers are recursively classified; they cannot bypass the
    # gateway merely by embedding an otherwise safe command.
    is_shell_wrapper = executable in ("bash", "sh", "zsh") and any(
        flag in ("-c", "-lc", "-cl") for flag in lower_argv[1:]
    )
    is_inline_runtime = (
        executable in ("python", "python3") and len(argv) >= 2 and argv[1] in ("-c", "-m")
    ) or (executable in ("node", "nodejs") and len(argv) >= 2 and argv[1] in ("-e", "--eval"))

    has_shell_compound = not is_inline_runtime and (
        any(token in (";", "&&", "||", "|") or token.endswith(";") for token in argv)
        or any(sep in raw_command for sep in ("$(", "`", ";", "&&", "||", "|"))
    )

    if is_shell_wrapper or has_shell_compound:
        inner = argv[-1] if is_shell_wrapper and len(argv) >= 3 else raw_command
        segments = _shell_segments(inner)
        if segments is not None:
            nested = [
                classify_action(
                    "shell.exec",
                    {"command": segment},
                    session,
                    cwd,
                    service_registry=registry,
                )
                for segment in segments
            ]
            _SAFE_DEV_TOOLS = frozenset(
                {"pytest", "ruff", "mypy", "pnpm", "npm", "uv", "cargo", "go", "tsc", "eslint"}
            )
            has_pipe = any(tok in ("|", "$(", "`") for tok in argv) or any(
                sep in raw_command for sep in ("|", "$(", "`")
            )
            all_safe_dev = not has_pipe and all(
                item.category == ActionCategory.AUTO_OPEN and item.target in _SAFE_DEV_TOOLS
                for item in nested
            )
            if all_safe_dev:
                cap = "shell.wrapper"
                op_hash = compute_operation_hash(norm_op, cwd, cap)
                return ActionClassification(
                    ActionCategory.AUTO_OPEN,
                    cap,
                    normalized_operation=norm_op,
                    operation_hash=op_hash,
                    target=executable,
                    reason="shell wrapper contains only safe development tool operations",
                )
            gated = next(
                (item for item in nested if item.category != ActionCategory.AUTO_OPEN),
                None,
            )
            if gated is not None and gated.category == ActionCategory.DENY:
                return gated
        cap = "privileged.shell_wrapper"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.REQUIRE_APPROVAL,
            cap,
            risk_class=RiskClass.P1_PRIVILEGED_HOST,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=executable,
            reason="shell wrapper could not be reduced to safe argv operations",
        )

    # Rule 16 & 17: Sudo and Root Shell -> P2_ROOT_MUTATION or P3_CRITICAL_HOST
    if executable == "sudo":
        sudo_args = lower_argv[1:]
        # Root shell detection
        if any(flag in ("-i", "-s") for flag in sudo_args) or (
            sudo_args and sudo_args[0] in ("bash", "sh", "zsh", "su")
        ):
            cap = "privileged.root_shell"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.HUMAN_GATED,
                cap,
                risk_class=RiskClass.P3_CRITICAL_HOST,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target="root_shell",
                reason="interactive root shell request",
            )
        cap = "privileged.sudo"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.HUMAN_GATED,
            cap,
            risk_class=RiskClass.P2_ROOT_MUTATION,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target="sudo",
            reason="sudo command requires human approval",
        )

    if executable == "su":
        cap = "privileged.root_shell"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.HUMAN_GATED,
            cap,
            risk_class=RiskClass.P3_CRITICAL_HOST,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target="su",
            reason="substitute user request",
        )

    # Permanent deny: host-root mutation and credential discovery are not
    # approval-capable actions.
    if re.search(
        r"rm\s+-[^ ]*\s+/\s*$|(?:touch|mkdir)\s+/(?:etc|boot|usr|var|root)(?:/|$)",
        raw_command,
        re.I,
    ):
        cap = "denied.host_root"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.DENY,
            cap,
            risk_class=RiskClass.P3_CRITICAL_HOST,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=executable,
            reason="host root mutation is permanently denied",
        )
    if re.search(
        r"(?:\.ssh|\.gnupg|browser.{0,20}(?:profile|cookie)|keyring|credential.store)",
        raw_command,
        re.I,
    ):
        cap = "denied.secret_discovery"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.DENY,
            cap,
            risk_class=RiskClass.P3_CRITICAL_HOST,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=executable,
            reason="credential discovery is permanently denied",
        )

    # Rule 3 & 3.1: Git (safe vs destructive)
    if executable == "git":
        subcmd = lower_argv[1] if len(lower_argv) > 1 else ""
        isolated_worktree = str(args.get("execution_target") or "").upper() in {
            "NEW_ISOLATED_WORKTREE",
            "EXISTING_WORKTREE",
            "WORKTREE",
        }
        canonical_target = not isolated_worktree
        destructive_git = False
        if (
            (subcmd == "reset" and "--hard" in lower_argv and canonical_target)
            or (
                subcmd == "clean"
                and canonical_target
                and any(f in lower_argv for f in ("-fd", "-fdx", "-f", "-x"))
            )
            or (
                subcmd == "push"
                and (
                    any(
                        f in lower_argv for f in ("--force", "-f", "--force-with-lease", "--delete")
                    )
                    or any(item.startswith(":") for item in lower_argv)
                )
            )
            or (
                subcmd == "branch"
                and (
                    any(f in argv for f in ("-D",))
                    or any(f in lower_argv for f in ("-d", "--delete"))
                )
            )
            or (subcmd == "tag" and any(f in lower_argv for f in ("-d", "--delete")))
        ):
            destructive_git = True

        if destructive_git:
            cap = "privileged.git_destructive"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.HUMAN_GATED,
                cap,
                risk_class=RiskClass.P2_ROOT_MUTATION,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target="git",
                reason="destructive git operation: history rewrite or data loss risk",
            )
        cap = "git.normal"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.AUTO_OPEN,
            cap,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target="git",
            reason="safe git workflow operation",
        )

    # Normal file lifecycle inside the already-bound workspace is development,
    # not a host destructive action. Absolute paths are accepted only when
    # they resolve below the gateway cwd. Destructive rm/rmdir commands fall
    # through to Rule 4b.
    if executable in {"unlink"}:
        targets = [Path(item) for item in argv[1:] if not item.startswith("-")]
        if targets and all(
            (target if target.is_absolute() else Path(cwd) / target).resolve(strict=False)
            in {candidate.resolve(strict=False) for candidate in (Path(cwd),)}
            or Path(cwd).resolve(strict=False)
            in (target if target.is_absolute() else Path(cwd) / target)
            .resolve(strict=False)
            .parents
            for target in targets
        ):
            cap = "workspace.file_lifecycle"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.AUTO_OPEN,
                cap,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target=executable,
                reason="file lifecycle remains inside authorized workspace",
            )

    # Rule 9, 10, 11, 18, 19, 34: systemd.  The parser is shared with the
    # executor so flags and global-option placement cannot create a second
    # policy dialect.
    if executable == "systemctl":
        invocation = parse_systemctl_argv(argv)
        if invocation is not None:
            read_action = invocation.action in _SYSTEMCTL_READ_ACTIONS
            read_safe = read_action and systemctl_read_options_valid(invocation)
            if invocation.scope == "user":
                unit_managed = invocation.unit is not None and registry.is_managed_unit(
                    invocation.unit
                )
                read_safe_user = read_safe and (invocation.unit is None or unit_managed)
                mutation_safe = (
                    invocation.action == "daemon-reload"
                    and invocation.unit is None
                    and not invocation.options
                ) or (
                    invocation.action in _SYSTEMCTL_USER_MUTATIONS
                    and unit_managed
                    and not invocation.options
                )
                if read_safe_user or mutation_safe:
                    cap = "service_control.user"
                    op_hash = compute_operation_hash(norm_op, cwd, cap)
                    return ActionClassification(
                        ActionCategory.AUTO_OPEN,
                        cap,
                        normalized_operation=norm_op,
                        operation_hash=op_hash,
                        target=invocation.unit or invocation.action,
                        reason=f"user systemd {invocation.action}",
                    )
                cap = "privileged.system_service_write"
                op_hash = compute_operation_hash(norm_op, cwd, cap)
                return ActionClassification(
                    ActionCategory.REQUIRE_APPROVAL,
                    cap,
                    risk_class=RiskClass.P2_ROOT_MUTATION,
                    normalized_operation=norm_op,
                    operation_hash=op_hash,
                    target=invocation.unit or "systemctl",
                    reason="unmanaged or unsupported user systemd operation",
                )

            if read_safe:
                cap = "system_service.read"
                op_hash = compute_operation_hash(norm_op, cwd, cap)
                return ActionClassification(
                    ActionCategory.AUTO_OPEN,
                    cap,
                    normalized_operation=norm_op,
                    operation_hash=op_hash,
                    target=invocation.unit or invocation.action,
                    reason="read-only system service inspection",
                )
            clean_unit = invocation.unit or "systemctl"
            if not clean_unit.endswith(".service") and clean_unit != "systemctl":
                clean_unit = f"{clean_unit}.service"
            cap = (
                "privileged.critical_service"
                if clean_unit in _CRITICAL_SERVICES
                else "privileged.system_service_write"
            )
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.REQUIRE_APPROVAL,
                cap,
                risk_class=(
                    RiskClass.P3_CRITICAL_HOST
                    if cap == "privileged.critical_service"
                    else RiskClass.P2_ROOT_MUTATION
                ),
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target=clean_unit,
                reason="system service mutation requires human approval",
            )

    # Rule 12 & 20: Journalctl
    if executable == "journalctl":
        # Mutation flags
        if any(
            flag in lower_argv
            for flag in (
                "--vacuum-size",
                "--vacuum-time",
                "--vacuum-files",
                "--rotate",
                "--flush",
            )
        ):
            cap = "privileged.host_write"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.HUMAN_GATED,
                cap,
                risk_class=RiskClass.P2_ROOT_MUTATION,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target="journalctl",
                reason="journal log modification/vacuum",
            )
        if "--user" in lower_argv:
            cap = "service_logs.user"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.AUTO_OPEN,
                cap,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target="journalctl",
                reason="read-only user service logs",
            )
        cap = "system_logs.read"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.AUTO_OPEN,
            cap,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target="journalctl",
            reason="read-only system logs inspection",
        )

    # Rule 14: Docker
    if executable == "docker":
        subcmd = lower_argv[1] if len(lower_argv) > 1 else ""
        # Privileged docker flags
        if (
            any(
                f in lower_argv
                for f in (
                    "--privileged",
                    "--pid=host",
                    "--network=host",
                    "-v /:/host",
                    "--ipc=host",
                )
            )
            or any("/:/host" in a for a in lower_argv)
            or (subcmd == "system" and "prune" in lower_argv)
            or (subcmd == "volume" and "prune" in lower_argv)
            or (subcmd == "image" and "prune" in lower_argv and "-a" in lower_argv)
        ):
            cap = "privileged.docker_privileged"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.HUMAN_GATED,
                cap,
                risk_class=RiskClass.P2_ROOT_MUTATION,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target="docker",
                reason="privileged or destructive docker operation",
            )

        # Read-only docker
        if subcmd in ("ps", "inspect", "logs", "stats", "images", "version", "info"):
            cap = "docker.read"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.AUTO_OPEN,
                cap,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target="docker",
                reason="read-only docker state query",
            )
        if subcmd == "compose" and any(c in lower_argv for c in ("ps", "logs", "config")):
            cap = "docker.read"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.AUTO_OPEN,
                cap,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target="docker compose",
                reason="read-only docker compose query",
            )

        # Project-scoped compose build/up/down/restart
        if subcmd == "compose" and any(
            c in lower_argv for c in ("build", "up", "down", "stop", "restart", "start")
        ):
            cap = "docker.project"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.AUTO_OPEN,
                cap,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target="docker compose",
                reason="project-scoped docker compose lifecycle",
            )
        if subcmd == "build":
            cap = "docker.project"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.AUTO_OPEN,
                cap,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target="docker build",
                reason="docker build within project context",
            )

    # Rule 15: Host package manager (apt, dpkg, snap)
    if executable in ("apt", "apt-get", "apt-cache", "dpkg", "snap"):
        if (
            executable in ("apt-cache",)
            or (executable == "apt" and any(sub in lower_argv for sub in ("list", "search")))
            or (executable == "dpkg" and any(sub in lower_argv for sub in ("-l", "--list")))
        ):
            cap = "host.package_read"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.AUTO_OPEN,
                cap,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target=executable,
                reason="read-only package queries",
            )
        cap = "privileged.host_package_manager"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.HUMAN_GATED,
            cap,
            risk_class=RiskClass.P2_ROOT_MUTATION,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=executable,
            reason="host package management mutation",
        )

    # Rule 25: Network management
    if executable in ("iptables", "nft", "ufw", "firewall-cmd"):
        cap = "privileged.network_admin"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.HUMAN_GATED,
            cap,
            risk_class=RiskClass.P2_ROOT_MUTATION,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=executable,
            reason="firewall or network policy mutation",
        )
    if executable in ("ip", "nmcli"):
        mutation = False
        if (
            executable == "ip"
            and any(m in lower_argv for m in ("set", "add", "del", "change", "replace"))
        ) or (
            executable == "nmcli"
            and any(m in lower_argv for m in ("modify", "edit", "delete", "up", "down"))
        ):
            mutation = True
        if mutation:
            cap = "privileged.network_admin"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.HUMAN_GATED,
                cap,
                risk_class=RiskClass.P2_ROOT_MUTATION,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target=executable,
                reason="host network configuration mutation",
            )
        cap = "host.diagnostics"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.AUTO_OPEN,
            cap,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=executable,
            reason="read-only network diagnostic",
        )

    # Rule 26: Storage & Filesystem administration
    if executable in ("mount", "umount", "mkfs", "fdisk", "parted", "resize2fs", "lvm"):
        if executable == "mount" and len(argv) == 1:
            cap = "host.diagnostics"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.AUTO_OPEN,
                cap,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target=executable,
                reason="mount listing",
            )
        cap = "privileged.storage_admin"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.HUMAN_GATED,
            cap,
            risk_class=RiskClass.P3_CRITICAL_HOST,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=executable,
            reason="host storage or filesystem mutation",
        )

    # Rule 27: User & Account administration
    if executable in (
        "useradd",
        "userdel",
        "usermod",
        "passwd",
        "groupadd",
        "groupdel",
        "groupmod",
        "chpasswd",
    ):
        cap = "privileged.account_admin"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.HUMAN_GATED,
            cap,
            risk_class=RiskClass.P3_CRITICAL_HOST,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=executable,
            reason="user account or credential administration",
        )

    # Rule 7: Python / Node inline execution
    if executable in ("python", "python3") and len(argv) >= 2 and argv[1] in ("-c", "-m"):
        cap = "shell.inline_runtime"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.AUTO_OPEN,
            cap,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=executable,
            reason="python inline runtime execution",
        )
    if executable in ("node", "nodejs") and len(argv) >= 2 and argv[1] in ("-e", "--eval"):
        cap = "shell.inline_runtime"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.AUTO_OPEN,
            cap,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=executable,
            reason="node inline runtime execution",
        )

    # Rule 21 & 27: Host diagnostics read-only
    if executable in (
        "ps",
        "id",
        "getent",
        "who",
        "ss",
        "lsblk",
        "findmnt",
        "df",
        "du",
        "loginctl",
        "resolvectl",
    ):
        cap = "host.diagnostics"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.AUTO_OPEN,
            cap,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=executable,
            reason="read-only host diagnostic",
        )

    # Rule 28: Scheduled tasks
    if executable == "crontab":
        if "-l" in lower_argv:
            cap = "schedule.user"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.AUTO_OPEN,
                cap,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target="crontab",
                reason="user crontab listing",
            )
        cap = "privileged.host_write"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.HUMAN_GATED,
            cap,
            risk_class=RiskClass.P2_ROOT_MUTATION,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target="crontab",
            reason="crontab mutation requires human approval",
        )

    # Rule 4b: Destructive shell commands. Reuse the canonical destructive
    # classifier (workspace_policy.classify_destructive) so generic shell
    # argv never silently AUTO_OPENs a destructive command. Specific rules
    # above (sudo / git destructive / systemctl / package managers ...) keep
    # their own capability ids; this only catches the remaining destructive
    # file/system operations (rm, rmdir, shred, dd, mkfs, chmod, chown,
    # kill, truncate, credential writes, ...).
    destructive_pattern = classify_destructive(raw_command)
    if destructive_pattern is not None:
        cap = "privileged.destructive_shell"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.HUMAN_GATED,
            cap,
            risk_class=RiskClass.P2_ROOT_MUTATION,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=executable,
            reason=f"destructive shell command ({destructive_pattern}) requires human approval",
        )

    # Rule 4 & 8: Normal argv development, testing, build, package commands
    # (python, node, npm, pnpm, uv, pytest, ruff, mypy, rg, find, sed, cat, cp, mv, mkdir, touch, curl, wget, etc.)
    cap = "shell.argv"
    op_hash = compute_operation_hash(norm_op, cwd, cap)
    return ActionClassification(
        ActionCategory.AUTO_OPEN,
        cap,
        normalized_operation=norm_op,
        operation_hash=op_hash,
        target=executable,
        reason="normal development argv command execution",
    )


class ActionGateway:
    """Gateway intercepting tool requests to enforce AUTO_OPEN and HUMAN_GATED policies."""

    def __init__(
        self,
        approval_store: ApprovalStore | None = None,
        service_registry: ManagedUserServiceRegistry | None = None,
    ) -> None:
        self.approval_store = approval_store or get_approval_store()
        self.service_registry = service_registry or _GLOBAL_SERVICE_REGISTRY
        self.permission_engine = PermissionEngine()

    def _engine_classification(
        self,
        tool_name: str,
        args: dict[str, Any],
        session: RemoteSession,
        cwd: str,
    ) -> ActionClassification:
        """Translate one remote request into the canonical engine contract."""
        root = Path(session.active_workspace or cwd).expanduser().resolve()
        current = Path(cwd).expanduser().resolve()
        raw_path = args.get("path")
        target_paths: tuple[Path, ...] = ()
        if raw_path:
            candidate = Path(str(raw_path))
            target_paths = ((candidate if candidate.is_absolute() else current / candidate),)
        if tool_name in {"file.read", "file.search", "artifact.read"}:
            context = OperationContext(
                actor=session.principal,
                tool=tool_name,
                operation=tool_name,
                workspace_root=root,
                cwd=current,
                target_paths=target_paths,
                filesystem_effect="read",
                session_id=session.session_id,
            )
        elif tool_name in {"file.write", "file.patch", "artifact.write"}:
            context = OperationContext(
                actor=session.principal,
                tool=tool_name,
                operation=tool_name,
                workspace_root=root,
                cwd=current,
                target_paths=target_paths,
                filesystem_effect="write",
                session_id=session.session_id,
            )
        elif tool_name == "shell.exec":
            context = parse_command_context(
                str(args.get("command", "")), cwd=current, workspace_root=root
            )
            context = OperationContext(
                **{
                    **context.__dict__,
                    "actor": session.principal,
                    "tool": tool_name,
                    "session_id": session.session_id,
                }
            )
        elif tool_name.startswith("worker.") or tool_name in {"hicode.execute", "agy.execute"}:
            worker_name = str(args.get("worker") or args.get("executor") or "hicode")
            context = OperationContext(
                actor=session.principal,
                tool=tool_name,
                operation=tool_name,
                workspace_root=root,
                cwd=current,
                filesystem_effect="write",
                session_id=session.session_id,
                executor_identity=get_executor_registry().identity(worker_name),
            )
        else:
            context = OperationContext(
                actor=session.principal,
                tool=tool_name,
                operation=tool_name,
                workspace_root=root,
                cwd=current,
                filesystem_effect="read",
                session_id=session.session_id,
            )
        decision = self.permission_engine.evaluate(context)
        capability = {
            ReasonCode.ALLOW_READ_ONLY: "workspace.read",
            ReasonCode.ALLOW_PROJECT_MUTATION: "workspace.file_write",
            ReasonCode.ALLOW_PROJECT_GIT: "git.normal",
            ReasonCode.ALLOW_USER_RUNTIME: "service.user",
            ReasonCode.ALLOW_NETWORK: "network.normal",
            ReasonCode.ALLOW_WORKER: "worker.dispatch",
            ReasonCode.APPROVAL_IRREVERSIBLE_REMOTE: "privileged.git_destructive",
            ReasonCode.APPROVAL_HOST_PRIVILEGE: "privileged.host",
            ReasonCode.APPROVAL_HOST_DESTRUCTIVE: "privileged.destructive",
            ReasonCode.APPROVAL_SECURITY_BOUNDARY: "security.boundary",
            ReasonCode.APPROVAL_UNKNOWN_HIGH_IMPACT: "privileged.unknown",
            ReasonCode.DENY_SCOPE_ESCAPE: "denied.scope_escape",
            ReasonCode.DENY_PATH_TRAVERSAL: "denied.path_traversal",
            ReasonCode.DENY_AUTHORITY_VIOLATION: "denied.authority",
            ReasonCode.DENY_INVALID_APPROVAL: "denied.invalid_approval",
        }.get(decision.reason, "permission.operation")
        command_words = tuple(context.command or ())
        if command_words:
            executable = Path(command_words[0]).name.lower()
            if executable == "sudo":
                capability = "privileged.sudo"
            elif executable in {"su"}:
                capability = "privileged.root_shell"
            elif context.service_effect == "system":
                capability = "privileged.system_service"
            elif (
                tool_name == "shell.exec"
                and decision.reason is ReasonCode.APPROVAL_HOST_DESTRUCTIVE
            ):
                # Canonical precedence: a destructive shell command is gated on
                # the session's destructive capability, so a session without it is
                # POLICY_BLOCKED outright rather than merely "awaiting approval".
                # This is the same capability the pre-existing classify_action
                # rules use, so both paths agree on what "destructive" means.
                capability = "privileged.destructive_shell"
        operation = decision.normalized_operation or tool_name
        if context.command:
            operation = " ".join(context.command)
        category = {
            Decision.ALLOW: ActionCategory.AUTO_OPEN,
            Decision.APPROVAL_REQUIRED: ActionCategory.REQUIRE_APPROVAL,
            Decision.DENY: ActionCategory.DENY,
        }[decision.decision]
        risk = RiskClass.P2_ROOT_MUTATION if category == ActionCategory.REQUIRE_APPROVAL else None
        return ActionClassification(
            category,
            capability,
            risk_class=risk,
            normalized_operation=operation,
            operation_hash=compute_operation_hash(operation, str(current), capability),
            target=str(raw_path or tool_name),
            reason=decision.reason.value,
        )

    def check_action(
        self,
        tool_name: str,
        args: dict[str, Any],
        session: RemoteSession,
        cwd: str,
    ) -> tuple[bool, RemoteErrorCode | None, str | None, ActionClassification]:
        """Verify if action is permitted to run, or requires approval."""
        classification = self._engine_classification(tool_name, args, session, cwd)

        if classification.category == ActionCategory.AUTO_OPEN:
            # Rule 37: approved field is ignored/not required for AUTO_OPEN
            return True, None, None, classification

        if classification.category == ActionCategory.DENY:
            return (
                False,
                RemoteErrorCode.POLICY_BLOCKED,
                classification.reason or "action permanently denied",
                classification,
            )

        # Destructive shell commands require the destructive capability in
        # addition to human approval. Without the capability the request is
        # POLICY_BLOCKED (BASE parity); with it, a server-issued approval_id
        # is still mandatory (boolean approved=true never bypasses this).
        if (
            classification.capability_id == "privileged.destructive_shell"
            and not session.permissions.destructive
        ):
            return (
                False,
                RemoteErrorCode.POLICY_BLOCKED,
                "destructive shell command requires the destructive capability",
                classification,
            )

        # Rule 38: HUMAN_GATED must reject boolean approval
        approved_flag = args.get("approved")
        approval_id = str(args.get("approval_id") or "").strip()

        if bool(approved_flag) and not approval_id:
            return (
                False,
                RemoteErrorCode.INVALID_APPROVAL,
                "boolean approved flag is rejected for privileged operations; server-issued approval_id required",
                classification,
            )

        if not approval_id:
            return (
                False,
                RemoteErrorCode.APPROVAL_REQUIRED,
                f"human approval required for {classification.capability_id} ({classification.risk_class})",
                classification,
            )

        # Rule 31 & 32: Validate single-use approval record
        ok, err_code, err_msg = self.approval_store.verify_and_consume(
            approval_id,
            principal=session.principal,
            capability_id=classification.capability_id,
            normalized_operation=classification.normalized_operation,
            cwd=cwd,
            workspace=session.active_workspace,
        )
        return ok, err_code, err_msg, classification
