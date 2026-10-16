"""W13 remote host registry (flag VEYA_REMOTE_CONTROL=1).

One record per tailnet machine Veya may drive. Durable state lives in a single
JSON map written through the repository's one atomic writer
(``server/remote_control/_ledger.py``); this module owns no I/O of its own.

Owner scoping is applied on every read a route or tool performs. The store is
**not** an authorisation boundary — it is bookkeeping; see
``server/remote_control/routes.py`` for the 404-not-403 ownership checks.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from server.remote_control import _ledger

#: Conservative id charset. Deliberately rejects ``:`` and ``@`` so a host id
#: can never be confused with a URL authority or an email address — the same
#: rule ``server/bot_team/runners.py:43`` applies to runner ids.
HOST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

#: Default executor command templates, by agent label. ``{task}`` and
#: ``{workspace}`` are placeholders; each is substituted as ONE argv element
#: (see ``executor.render_command``).
DEFAULT_AGENT_COMMANDS: dict[str, str] = {
    "pi": "pi --run {task}",
    "claude-wb": "claude-wb -p {task}",
    "codex": "codex exec {task}",
}

DEFAULT_AGENT_LABEL = "pi"

_LEDGER_ENV = "VEYA_REMOTE_CONTROL_HOSTS_PATH"
_LEDGER_FILE = "remote_control_hosts.json"


#: W17 ssh fields. ``ssh_key_ref`` is the NAME of a vault secret, never a key
#: or a path, so it may not contain ``/``, ``\\``, ``:`` or whitespace.
SSH_KEY_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
OS_FAMILIES = frozenset({"linux", "windows-wsl", "macos"})
HOST_KEY_POLICIES = frozenset({"accept-new", "strict"})


class HostValidationError(ValueError):
    """A host record was rejected before it reached the ledger."""


@dataclass
class RemoteHost:
    """One machine on the tailnet that Veya may drive."""

    host_id: str
    display_name: str
    tailnet_ip: str
    agent_command: str = ""
    agent_label: str = DEFAULT_AGENT_LABEL
    owner_id: str = ""
    workspace_path: str = ""
    enabled: bool = True
    last_seen_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    # ── W17 ssh fields (all defaulted: W13 records load unchanged) ──
    ssh_user: str = ""
    ssh_port: int = 22
    ssh_key_ref: str = ""
    ssh_host_key_policy: str = "accept-new"
    os_family: str = "linux"
    write_root: str = ""
    default_cwd: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # W17 fields left at their defaults are omitted, so a W13 record (and
        # every W13 response) stays byte-identical while ssh hosts are unused.
        for key, default in _SSH_DEFAULTS.items():
            if data.get(key) == default:
                data.pop(key, None)
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RemoteHost:
        label = str(value.get("agent_label") or DEFAULT_AGENT_LABEL)
        meta = value.get("metadata")
        return cls(
            host_id=str(value.get("host_id") or ""),
            display_name=str(value.get("display_name") or value.get("host_id") or ""),
            tailnet_ip=str(value.get("tailnet_ip") or ""),
            agent_command=str(value.get("agent_command") or ""),
            agent_label=label,
            owner_id=str(value.get("owner_id") or ""),
            workspace_path=str(value.get("workspace_path") or ""),
            enabled=bool(value.get("enabled", True)),
            last_seen_at=(
                float(value["last_seen_at"]) if value.get("last_seen_at") is not None else None
            ),
            metadata=dict(meta) if isinstance(meta, dict) else {},
            created_at=float(value.get("created_at") or time.time()),
            ssh_user=str(value.get("ssh_user") or ""),
            ssh_port=_as_port(value.get("ssh_port")),
            ssh_key_ref=str(value.get("ssh_key_ref") or ""),
            ssh_host_key_policy=str(value.get("ssh_host_key_policy") or "accept-new"),
            os_family=str(value.get("os_family") or "linux"),
            write_root=str(value.get("write_root") or ""),
            default_cwd=str(value.get("default_cwd") or ""),
        )

    def command_template(self) -> str:
        """The operator template, or the built-in default for the label."""

        template = self.agent_command.strip()
        if template:
            return template
        return DEFAULT_AGENT_COMMANDS.get(
            self.agent_label, DEFAULT_AGENT_COMMANDS[DEFAULT_AGENT_LABEL]
        )


_SSH_DEFAULTS: dict[str, Any] = {
    "ssh_user": "",
    "ssh_port": 22,
    "ssh_key_ref": "",
    "ssh_host_key_policy": "accept-new",
    "os_family": "linux",
    "write_root": "",
    "default_cwd": "",
}


def _as_port(value: Any) -> int:
    try:
        return int(value) if value not in (None, "") else 22
    except (TypeError, ValueError):
        return 0  # rejected by validate_host


def _is_remote_abs(path: str, os_family: str) -> bool:
    if path.startswith("/"):
        return True
    return os_family == "windows-wsl" and bool(re.match(r"^[A-Za-z]:\\", path))


def validate_ssh_fields(host: RemoteHost) -> RemoteHost:
    """W17: reject ssh connection fields that could not be used safely."""

    if host.os_family not in OS_FAMILIES:
        raise HostValidationError("os_family must be one of linux, windows-wsl, macos")
    if not 1 <= int(host.ssh_port or 0) <= 65535:
        raise HostValidationError("ssh_port must be 1..65535")
    if host.ssh_key_ref and not SSH_KEY_REF_RE.match(host.ssh_key_ref):
        raise HostValidationError(
            "ssh_key_ref must be a vault secret name (^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$), "
            "never a key or a path"
        )
    if host.ssh_host_key_policy not in HOST_KEY_POLICIES:
        raise HostValidationError("ssh_host_key_policy must be accept-new or strict")
    if host.ssh_user and not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$", host.ssh_user):
        raise HostValidationError("ssh_user has invalid characters")
    for name in ("write_root", "default_cwd"):
        value = str(getattr(host, name) or "")
        if not value:
            continue
        if any(ch in value for ch in ("\n", "\r", "\x00")):
            raise HostValidationError(f"{name} must be a single line")
        if not _is_remote_abs(value, host.os_family):
            raise HostValidationError(f"{name} must be an absolute remote path")
        if name == "write_root" and value.rstrip("/\\") in ("", "C:", "c:"):
            raise HostValidationError("write_root must not be the filesystem root")
    return host


def validate_host(host: RemoteHost) -> RemoteHost:
    """Reject a host that could not be driven safely. Raises on refusal."""

    if not HOST_ID_RE.match(host.host_id):
        raise HostValidationError(
            "host_id must match ^[a-z0-9][a-z0-9-]{0,62}$ (lowercase, digits, dash)"
        )
    if not host.tailnet_ip.strip():
        raise HostValidationError("tailnet_ip is required")
    if not host.owner_id.strip():
        raise HostValidationError("owner_id is required")
    validate_ssh_fields(host)
    return host


def _hosts_path():
    return _ledger.default_path(_LEDGER_ENV, _LEDGER_FILE)


def save_host(host: RemoteHost) -> RemoteHost:
    """Create or replace one host record."""

    validate_host(host)

    def _mutate(rows: dict[str, dict[str, Any]]):
        rows[host.host_id] = host.to_dict()
        return rows, host

    return _ledger.update(_hosts_path(), _mutate)


def get_host(host_id: str) -> RemoteHost | None:
    row = _ledger.read_map(_hosts_path()).get(str(host_id))
    return RemoteHost.from_dict(row) if row else None


def list_hosts(*, owner_id: str | None = None, enabled_only: bool = False) -> list[RemoteHost]:
    """List hosts, newest first, filtered by owner and/or enabled state."""

    rows = _ledger.read_map(_hosts_path())
    hosts = [RemoteHost.from_dict(row) for row in rows.values()]
    if owner_id is not None:
        hosts = [host for host in hosts if host.owner_id == owner_id]
    if enabled_only:
        hosts = [host for host in hosts if host.enabled]
    return sorted(hosts, key=lambda host: host.created_at, reverse=True)


def delete_host(host_id: str) -> bool:
    """Remove one host. False when it was already absent."""

    removed = False

    def _mutate(rows: dict[str, dict[str, Any]]):
        nonlocal removed
        removed = rows.pop(str(host_id), None) is not None
        return rows, None

    _ledger.update(_hosts_path(), _mutate)
    return removed


def touch_seen(host_id: str, *, now: float | None = None) -> RemoteHost | None:
    """Stamp ``last_seen_at``. Returns the updated record, or None if absent."""

    stamp = time.time() if now is None else float(now)

    def _mutate(rows: dict[str, dict[str, Any]]):
        row = rows.get(str(host_id))
        if row is None:
            return rows, None
        host = RemoteHost.from_dict(row)
        host.last_seen_at = stamp
        rows[host.host_id] = host.to_dict()
        return rows, host

    return _ledger.update(_hosts_path(), _mutate)


def set_enabled(host_id: str, enabled: bool) -> RemoteHost | None:
    """Flip the enabled flag without deleting the record."""

    def _mutate(rows: dict[str, dict[str, Any]]):
        row = rows.get(str(host_id))
        if row is None:
            return rows, None
        host = RemoteHost.from_dict(row)
        host.enabled = bool(enabled)
        rows[host.host_id] = host.to_dict()
        return rows, host

    return _ledger.update(_hosts_path(), _mutate)


__all__ = [
    "DEFAULT_AGENT_COMMANDS",
    "DEFAULT_AGENT_LABEL",
    "HOST_ID_RE",
    "HOST_KEY_POLICIES",
    "OS_FAMILIES",
    "SSH_KEY_REF_RE",
    "HostValidationError",
    "RemoteHost",
    "delete_host",
    "get_host",
    "list_hosts",
    "save_host",
    "set_enabled",
    "touch_seen",
    "validate_host",
    "validate_ssh_fields",
]
