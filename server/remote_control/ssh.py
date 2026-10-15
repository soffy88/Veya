# 3O-IO-ALLOW — server/remote_control/ssh.py
# 理由：W17 规格 §16 指定的两处物理 IO 之一：ControlPath / 私钥落地目录的一次性
# mkdir + 从 vault 取出的 PEM 以 0600 写盘（ssh -i 只接受文件路径）。进程执行
# 仍只走 server/remote_control/process.py 这一个 seam；测试注入假 runner，不 fork。
"""W17 — ssh connection layer for registered remote hosts.

Pure argv construction + one injectable runner seam:

* :func:`ssh_target` / :func:`ssh_base_argv` — fixed, safe ``ssh`` options
  (``BatchMode``, host-key policy, timeouts, ``ServerAlive``, ControlMaster
  reuse, ``--`` separator).
* :func:`run_remote` — run ONE remote argv (each element POSIX-quoted, so the
  remote shell sees exactly the argv we built — model text is never spliced
  into a command line).
* :func:`probe` — read-only reachability check (``uname -s``).

The private key is referenced by vault NAME (``host.ssh_key_ref``); its value
never appears in a ledger, a log or a tool result (outputs are masked).
"""

from __future__ import annotations

import os
import re
import shlex
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.remote_control import flags

Runner = Callable[..., Any]  # (argv, *, timeout_s, input_bytes) -> CaptureResult-like

_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]{0,252}$")
VAULT_PREFIX = "ssh_key:"


class SshError(RuntimeError):
    """An ssh call could not be built or did not complete. Safe to show."""


@dataclass
class RemoteResult:
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    raw_stdout: bytes = b""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@dataclass
class ProbeResult:
    reachable: bool
    latency_ms: float | None = None
    os_family_echo: str = ""
    error: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "latency_ms": self.latency_ms,
            "os_family_echo": self.os_family_echo,
            "error": self.error,
        }


# ── tuning ──────────────────────────────────────────────────────────────────


def connect_timeout() -> int:
    return flags.env_int("VEYA_SSH_HOSTS_CONNECT_TIMEOUT", 10)


def run_timeout() -> float:
    return flags.env_float("VEYA_SSH_HOSTS_RUN_TIMEOUT", 60.0)


def max_output() -> int:
    return flags.env_int("VEYA_SSH_HOSTS_MAX_OUTPUT", 8000)


def control_persist() -> int:
    return flags.env_int("VEYA_SSH_HOSTS_CONTROL_PERSIST", 600)


def keys_dir() -> Path:
    return Path(os.environ.get("VEYA_SSH_HOSTS_KEYS_DIR", "") or "~/.veya/ssh/keys").expanduser()


def cm_dir() -> Path:
    return Path(os.environ.get("VEYA_SSH_HOSTS_CM_DIR", "") or "~/.veya/ssh/cm").expanduser()


# ── argv ────────────────────────────────────────────────────────────────────


def ssh_target(host: Any) -> str:
    address = str(getattr(host, "tailnet_ip", "") or "").strip()
    if not _TARGET_RE.match(address):
        raise SshError("host address is not a valid ip / host name")
    user = str(getattr(host, "ssh_user", "") or "").strip()
    return f"{user}@{address}" if user else address


def _ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)  # 3O-IO-ALLOW
    return path


def ssh_base_argv(host: Any, *, key_path: str = "", control_dir: Path | None = None) -> list[str]:
    """Fixed ssh options + target; the caller appends ``["--", remote]``."""

    policy = (
        "yes" if getattr(host, "ssh_host_key_policy", "accept-new") == "strict" else "accept-new"
    )
    cm = control_dir if control_dir is not None else cm_dir()
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"StrictHostKeyChecking={policy}",
        "-o",
        f"ConnectTimeout={connect_timeout()}",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={cm}/cm-%r@%h:%p",
        "-o",
        f"ControlPersist={control_persist()}",
    ]
    if key_path:
        argv += ["-o", "IdentitiesOnly=yes", "-i", key_path]
    argv += ["-p", str(int(getattr(host, "ssh_port", 22) or 22)), ssh_target(host)]
    return argv


def quote_remote(argv: Sequence[str]) -> str:
    """POSIX-quote every element; refuse NUL / CR (no smuggled lines)."""

    parts = [str(item) for item in argv]
    if not parts:
        raise SshError("empty remote command")
    for item in parts:
        if "\x00" in item or "\r" in item:
            raise SshError("remote argv may not contain NUL or CR")
    return " ".join(shlex.quote(item) for item in parts)


# ── keys ────────────────────────────────────────────────────────────────────


def _vault_value(ref: str) -> str | None:
    from veya.platform import obase as _load_obase

    vault = _load_obase().secrets_store.SecretsStore(
        vault_dir=Path(os.environ.get("VEYA_VAULT_DIR", "") or "~/.veya/vault").expanduser()
    )
    name = VAULT_PREFIX + ref
    if not vault.has_secret(name):
        return None
    return str(vault.get_secret(name) or "")


def resolve_key_path(
    host: Any, *, lookup: Callable[[str], str | None] | None = None
) -> tuple[str, list[str]]:
    """Return ``(key_path, secret_values_to_mask)`` for ``host.ssh_key_ref``.

    Empty ref → ``("", [])`` (ssh's own default identities / agent). The vault
    value is either an absolute key-file path or a PEM; a PEM is materialised
    once at ``keys_dir()/<host_id>.pem`` with mode 0600.
    """

    ref = str(getattr(host, "ssh_key_ref", "") or "")
    if not ref:
        return "", []
    from server.remote_control.hosts import SSH_KEY_REF_RE

    if not SSH_KEY_REF_RE.match(ref):
        raise SshError("ssh_key_ref is not a vault secret name")
    value = (lookup or _vault_value)(ref)
    if not value:
        raise SshError(f"vault secret {VAULT_PREFIX}{ref} not found")
    value = value.strip()
    if value.startswith("-----BEGIN"):
        target = _ensure_private_dir(keys_dir()) / f"{host.host_id}.pem"
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 3O-IO-ALLOW
        with os.fdopen(fd, "w", encoding="utf-8") as handle:  # 3O-IO-ALLOW
            handle.write(value + "\n")
        return str(target), [value]
    if not value.startswith("/") or "\n" in value:
        raise SshError("vault ssh key value must be an absolute key path or a PEM")
    return value, []


# ── output hygiene ──────────────────────────────────────────────────────────


def clean_output(text: str, *, secrets: Sequence[str] = (), limit: int | None = None) -> str:
    from runtime.coding.command_runner import redact_text

    out = redact_text(text or "", secret_values=[s for s in secrets if s])
    cap = max_output() if limit is None else limit
    if len(out) > cap:
        out = out[:cap] + f"\n…[truncated {len(out) - cap} chars]"
    return out


# ── run ─────────────────────────────────────────────────────────────────────


def _default_runner(argv: list[str], *, timeout_s: float, input_bytes: bytes | None = None) -> Any:
    from server.remote_control import process

    return process.run_capture(argv, timeout_s=timeout_s, input_bytes=input_bytes)


def run_remote(
    host: Any,
    argv: Sequence[str],
    *,
    timeout_s: float | None = None,
    input_bytes: bytes | None = None,
    runner: Runner | None = None,
    key_lookup: Callable[[str], str | None] | None = None,
    output_limit: int | None = None,
) -> RemoteResult:
    """Run one remote argv over ssh and return masked, truncated output."""

    if getattr(host, "os_family", "linux") == "windows-wsl":
        # W17 slice: POSIX hosts only. Windows OpenSSH quoting (cmd/powershell/
        # wsl.exe) is a separate, unimplemented branch (spec §9.1, H8).
        raise SshError("windows-wsl hosts are not supported yet by the ssh backend")
    key_path, secrets = resolve_key_path(host, lookup=key_lookup)
    control = cm_dir()
    if runner is None:
        _ensure_private_dir(control)
    full = [*ssh_base_argv(host, key_path=key_path, control_dir=control), "--", quote_remote(argv)]
    try:
        result = (runner or _default_runner)(
            full, timeout_s=float(timeout_s or run_timeout()), input_bytes=input_bytes
        )
    except Exception as exc:  # ProcessUnavailable (no ssh binary) etc.
        raise SshError(f"ssh could not be run: {exc}") from exc
    raw_out = getattr(result, "stdout", b"") or b""
    raw_err = getattr(result, "stderr", b"") or b""
    if isinstance(raw_out, str):
        raw_out = raw_out.encode("utf-8")
    if isinstance(raw_err, str):
        raw_err = raw_err.encode("utf-8")
    return RemoteResult(
        returncode=getattr(result, "returncode", None),
        stdout=clean_output(
            raw_out.decode("utf-8", "replace"), secrets=secrets, limit=output_limit
        ),
        stderr=clean_output(raw_err.decode("utf-8", "replace"), secrets=secrets, limit=2000),
        timed_out=bool(getattr(result, "timed_out", False)),
        raw_stdout=raw_out,
    )


def probe(host: Any, *, runner: Runner | None = None, key_lookup=None) -> ProbeResult:
    """Read-only reachability check: ``ssh … -- uname -s`` (5 s bound)."""

    started = time.monotonic()
    try:
        result = run_remote(
            host, ["uname", "-s"], timeout_s=5.0, runner=runner, key_lookup=key_lookup
        )
    except SshError as exc:
        return ProbeResult(reachable=False, error=str(exc))
    latency = round((time.monotonic() - started) * 1000, 1)
    if not result.ok:
        err = (
            "timeout"
            if result.timed_out
            else (result.stderr.strip()[:200] or f"exit {result.returncode}")
        )
        return ProbeResult(reachable=False, latency_ms=latency, error=err)
    echo = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    family = {"Linux": "linux", "Darwin": "macos"}.get(echo, echo.lower())
    return ProbeResult(reachable=True, latency_ms=latency, os_family_echo=family)


__all__ = [
    "VAULT_PREFIX",
    "ProbeResult",
    "RemoteResult",
    "SshError",
    "clean_output",
    "probe",
    "quote_remote",
    "resolve_key_path",
    "run_remote",
    "ssh_base_argv",
    "ssh_target",
]
