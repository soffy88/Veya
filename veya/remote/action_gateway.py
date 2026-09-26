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

import shlex
from enum import StrEnum
from pathlib import Path
from typing import Any

from .approval import ApprovalStore, compute_operation_hash, get_approval_store
from .models import ManagedUserService, RemoteErrorCode, RemoteSession, RiskClass
from .workspace_policy import classify_destructive


class ActionCategory(StrEnum):
    AUTO_OPEN = "AUTO_OPEN"
    HUMAN_GATED = "HUMAN_GATED"


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
        return self.category == ActionCategory.HUMAN_GATED

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


def _parse_argv(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


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

    # Rule 6: Shell wrappers (bash -lc, sh -c, compound shell operators) -> P1_PRIVILEGED_HOST
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
        cap = "privileged.shell_wrapper"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.HUMAN_GATED,
            cap,
            risk_class=RiskClass.P1_PRIVILEGED_HOST,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=executable,
            reason="shell wrapper or compound operators allow unrestricted execution and expansion",
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

    # Rule 3 & 3.1: Git (safe vs destructive)
    if executable == "git":
        subcmd = lower_argv[1] if len(lower_argv) > 1 else ""
        destructive_git = False
        if (
            (subcmd == "reset" and "--hard" in lower_argv)
            or (subcmd == "clean" and any(f in lower_argv for f in ("-fd", "-fdx", "-f", "-x")))
            or (
                subcmd == "push"
                and any(f in lower_argv for f in ("--force", "-f", "--force-with-lease"))
            )
            or (
                subcmd == "branch"
                and any(f in lower_argv for f in ("-d", "--delete"))
                and any(f in lower_argv for f in ("-d", "-f", "--force"))
            )
            or (subcmd == "tag" and "-d" in lower_argv)
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

    # Rule 9, 10, 11: User-level systemd (systemctl --user ...)
    if executable == "systemctl" and len(argv) >= 2 and argv[1] == "--user":
        # daemon-reload
        if len(argv) == 3 and argv[2] == "daemon-reload":
            cap = "service_control.user"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.AUTO_OPEN,
                cap,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target="daemon-reload",
                reason="user systemd daemon-reload",
            )
        # unit actions (e.g. systemctl --user <action> <unit>)
        allowed_user_actions = (
            "start",
            "stop",
            "restart",
            "reload",
            "try-restart",
            "enable",
            "disable",
            "is-active",
            "is-enabled",
            "status",
            "show",
        )
        if len(argv) == 4:
            action = argv[2].lower()
            unit = argv[3]
            if action in allowed_user_actions and registry.is_managed_unit(unit):
                cap = "service_control.user"
                op_hash = compute_operation_hash(norm_op, cwd, cap)
                return ActionClassification(
                    ActionCategory.AUTO_OPEN,
                    cap,
                    normalized_operation=norm_op,
                    operation_hash=op_hash,
                    target=unit,
                    reason=f"managed user service control ({action})",
                )

        # Unmanaged unit or extra flags/arguments requires human approval
        cap = "privileged.system_service_write"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.HUMAN_GATED,
            cap,
            risk_class=RiskClass.P2_ROOT_MUTATION,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=argv[3] if len(argv) > 3 else "systemctl",
            reason="unmanaged user systemd command or extra arguments requires human approval",
        )

    # Rule 18, 19, 34: System-level systemd
    if executable == "systemctl" and "--user" not in lower_argv:
        # Check read-only queries
        read_only_actions = ("status", "show", "is-active", "is-enabled")
        action = ""
        unit = ""
        for item in argv[1:]:
            if not item.startswith("-"):
                if not action:
                    action = item.lower()
                elif not unit:
                    unit = item.lower()

        if action in read_only_actions:
            cap = "system_service.read"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.AUTO_OPEN,
                cap,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target=unit or "systemctl",
                reason="read-only system service inspection",
            )

        # Mutation: check critical service
        clean_unit = unit if unit.endswith(".service") else f"{unit}.service"
        if clean_unit in _CRITICAL_SERVICES:
            cap = "privileged.critical_service"
            op_hash = compute_operation_hash(norm_op, cwd, cap)
            return ActionClassification(
                ActionCategory.HUMAN_GATED,
                cap,
                risk_class=RiskClass.P3_CRITICAL_HOST,
                normalized_operation=norm_op,
                operation_hash=op_hash,
                target=clean_unit,
                reason="critical host system service mutation",
            )

        cap = "privileged.system_service_write"
        op_hash = compute_operation_hash(norm_op, cwd, cap)
        return ActionClassification(
            ActionCategory.HUMAN_GATED,
            cap,
            risk_class=RiskClass.P2_ROOT_MUTATION,
            normalized_operation=norm_op,
            operation_hash=op_hash,
            target=clean_unit or "systemctl",
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

    def check_action(
        self,
        tool_name: str,
        args: dict[str, Any],
        session: RemoteSession,
        cwd: str,
    ) -> tuple[bool, RemoteErrorCode | None, str | None, ActionClassification]:
        """Verify if action is permitted to run, or requires approval."""
        classification = classify_action(
            tool_name, args, session, cwd, service_registry=self.service_registry
        )

        if classification.category == ActionCategory.AUTO_OPEN:
            # Rule 37: approved field is ignored/not required for AUTO_OPEN
            return True, None, None, classification

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
