"""
Secure sandbox module — P1 core capability.
Features: resource limits, operation audit, automatic rollback, isolated execution.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

_DANGEROUS_PATTERNS: tuple[str, ...] = (
    r"rm\s+-rf",
    r"rm\s+--no-preserve-root",
    r"chmod\s+777",
    r"chown\s+-R",
    r"docker\s+rmi\s+-f",
    r"\|\s*rm",
    r"\|\s*mv",
    r"\|\s*dd",
    r"\|\s*format",
)

_DANGEROUS_SUBSTRINGS: tuple[str, ...] = (
    "reset --hard",
    "rebase -i",
    "push -f",
    "clean -fd",
    "filter-branch",
)

# SEC H5: 子进程环境变量白名单。绝不 ``os.environ.copy()`` —— 那会把服务端
# 所有 provider key / VEYA_* 配置送进 (可能被提示注入驱动的) 子进程。
_SANDBOX_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "PWD",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "HOSTNAME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
)
_SANDBOX_ENV_ALLOWLIST_PREFIXES: tuple[str, ...] = ("LC_", "LANG", "TZ")

_bwrap_probe: bool | None = None


def _bwrap_available() -> bool:
    """Whether bubblewrap can actually create a namespace here (cached probe).

    Presence on PATH is not enough (the kernel may disallow unprivileged user
    namespaces); probe once so the sandbox silently falls back to the
    proxy-scrub path instead of failing every command.
    """
    global _bwrap_probe
    if _bwrap_probe is not None:
        return _bwrap_probe
    bwrap = shutil.which("bwrap")
    if not bwrap:
        _bwrap_probe = False
        return False
    try:
        probe = subprocess.run(
            [bwrap, "--unshare-net", "--bind", "/", "/", "--", "true"],
            capture_output=True,
            timeout=5,
        )
        _bwrap_probe = probe.returncode == 0
    except Exception:
        _bwrap_probe = False
    return _bwrap_probe


# --- sandbox-runtime (srt) backend ---
# veya 的隔离层从手写 bwrap --unshare-net 切换为 Anthropic sandbox-runtime:
# srt 在 bwrap 之上再加文件白名单 + 域名白名单 + 凭证目录掩盖, 补齐旧实现
# 的三块短板: 旧实现 --bind / / 导致文件视图全开 (子进程可读 ~/.ssh、
# ~/.veya/auth.db 等宿主凭证)、网络只有"全断/全通"两档、bwrap 缺失时退回
# 纯代理 env 软阻断 (直接 socket 即可绕过)。
#
# 后端选择 (VEYA_SANDBOX_BACKEND):
#   auto   (默认) : srt 二进制在位就用 srt, 否则退回旧 bwrap 路径; 实际走哪条
#          路写进每次执行结果的 sandbox_backend 和审计日志, 不静默降级。
#   srt    : 强制 srt; 二进制缺失或配置无法表达 (如 network_blocked=False
#          且未给 allowed_domains) 时执行直接失败, 不退回旧路径。
#   legacy : 强制旧路径。
# VEYA_SRT_BIN 可指定 srt 二进制位置 (默认 PATH 查找)。
# VEYA_SRT_ALLOW_ALL_UNIX_SOCKETS=1 时 srt 跳过 Unix socket 创建封堵
# (apply-seccomp 嵌套 userns): ubuntu26 默认禁非特权 userns, 全量模式在该
# 主机上直接报 setgroups 权限错, 必须开此开关才能跑; 代价是沙箱内进程可
# 创建 Unix socket (容器内无 docker.sock 挂载, 残余面主要是宿主侧的
# /run/veya 等 socket, 容器部署可接受; 宿主部署须知悉)。默认 0 (全量)。
_SRT_CREDENTIAL_DENY_READ: tuple[str, ...] = (
    "~/.ssh",
    "~/.veya",
    "~/.claude",
    "~/.codex",
    "~/.grok",
    "~/.pi",
    "~/.opencode",
    "~/.local/share/opencode/auth.json",
)


def _srt_binary() -> str | None:
    override = os.environ.get("VEYA_SRT_BIN", "").strip()
    if override:
        return override if os.path.isfile(override) else None
    return shutil.which("srt")


_SRT_PROBE_CACHE: dict[tuple[str, bool], bool] = {}


def _srt_probe(binary: str) -> bool:
    """Run one trivial srt command to prove srt can actually sandbox here.

    Binary presence is not enough: inside a Docker container bwrap cannot
    create namespaces at all, so every srt run fails even with the binary
    installed. The probe result is cached per (binary, allow-all-flag) so
    the ~0.3s probe cost is paid once per process. A probe failure only
    disables auto mode; explicit mode=srt still fails hard on the real run.
    """
    key = (binary, _srt_allow_all_unix_sockets())
    if key in _SRT_PROBE_CACHE:
        return _SRT_PROBE_CACHE[key]
    ok = False
    try:
        with tempfile.TemporaryDirectory(prefix="veya_srt_probe_") as work_dir:
            probe_config = SandboxConfig(network_blocked=True, allow_write=False)
            settings = build_srt_settings(probe_config, work_dir=work_dir)
            fd, path = tempfile.mkstemp(prefix="veya_srt_probe_", suffix=".json")
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(settings, handle)
                proc = subprocess.run(
                    [binary, "--settings", path, "--", "true"],
                    capture_output=True,
                    timeout=30,
                )
                ok = proc.returncode == 0
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(path)
    except Exception:
        ok = False
    _SRT_PROBE_CACHE[key] = ok
    return ok


def _srt_backend_mode() -> str:
    return os.environ.get("VEYA_SANDBOX_BACKEND", "auto").strip().lower() or "auto"


def _srt_allow_all_unix_sockets() -> bool:
    return os.environ.get("VEYA_SRT_ALLOW_ALL_UNIX_SOCKETS", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _srt_enabled_for(config: SandboxConfig) -> bool:
    mode = _srt_backend_mode()
    if mode == "srt":
        return True
    if mode == "legacy":
        return False
    binary = _srt_binary()
    if binary is None:
        return False
    # auto 模式: 二进制在位还不够, 必须探活成功 (容器内 bwrap 建不了
    # namespace 时探活失败, 自动退回旧路径, 而不是让每次执行都报错)。
    return _srt_probe(binary)


def build_srt_settings(
    config: SandboxConfig,
    *,
    work_dir: str,
) -> dict[str, Any]:
    """从 SandboxConfig 生成 srt settings (纯函数, 可单测)。

    fail-closed 两点:
    - network_blocked=False 且未给 allowed_domains 时抛错: srt 的
      allowedDomains 明确拒绝 "*" 这类全通 pattern, 旧"全通"语义无法表达,
      绝不能静默翻译成 [] (那会变成断网) 或全通 (那会丢隔离)。
    - allowWrite 只含本次沙箱工作目录 (allow_write 时再加 /tmp);
      宿主 home、项目、repo 一律不在写白名单内。
    """
    if not config.network_blocked and not config.allowed_domains:
        raise ValueError(
            "srt backend cannot express unrestricted network: "
            "set SandboxConfig.allowed_domains or use network_blocked=True"
        )
    allow_write = [work_dir]
    if config.allow_write:
        allow_write.append("/tmp")
    return {
        "network": {
            "allowedDomains": [] if config.network_blocked else list(config.allowed_domains or []),
            "deniedDomains": [],
            "allowUnixSockets": [],
            "allowAllUnixSockets": _srt_allow_all_unix_sockets(),
        },
        "filesystem": {
            "denyRead": list(_SRT_CREDENTIAL_DENY_READ),
            "allowRead": [],
            "allowWrite": allow_write,
            "denyWrite": [],
        },
    }


def is_dangerous_command(command: str) -> bool:
    """Dangerous-command detection (canonical single source, §1.4: tools.py delegates here)."""
    lowered = (command or "").lower()
    return any(re.search(pattern, lowered) for pattern in _DANGEROUS_PATTERNS) or any(
        marker in lowered for marker in _DANGEROUS_SUBSTRINGS
    )


def is_dangerous_argv(argv: list[str]) -> bool:
    """Dangerous detection for argv form (rebuilds a quoted string, then reuses the canonical check)."""
    return is_dangerous_command(" ".join(shlex.quote(a) for a in argv))


class SandboxStatus(StrEnum):
    """Sandbox status enum."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"


class ResourceType(StrEnum):
    """Resource type."""

    MEMORY = "memory"
    CPU = "cpu"
    DISK = "disk"
    NETWORK = "network"
    TIME = "time"


@dataclass
class ResourceLimit:
    """Resource limit definition."""

    type: ResourceType
    limit: int | float
    unit: str = ""
    soft: bool = False  # 是否软限制

    def __str__(self) -> str:
        soft_suffix = " (soft)" if self.soft else ""
        return f"{self.limit}{self.unit} {self.type.value}{soft_suffix}"


@dataclass
class AuditLog:
    """Audit log entry."""

    timestamp: float = field(default_factory=time.time)
    action: str = ""
    resource: str = ""
    status: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    user: str = "system"
    session_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "action": self.action,
            "resource": self.resource,
            "status": self.status,
            "details": self.details,
            "user": self.user,
            "session_id": self.session_id,
        }


@dataclass
class SandboxConfig:
    """Sandbox configuration."""

    memory_limit: int | None = None  # in bytes
    cpu_limit: float | None = None  # in CPU seconds (RLIMIT_CPU, soft)
    time_limit: float | None = None  # in seconds (wall clock)
    disk_limit: int | None = None  # in bytes
    network_blocked: bool = True
    working_dir: str | None = None
    allow_write: bool = False
    audit_enabled: bool = True
    reject_dangerous: bool = True  # G4: 危险命令执行前拦截
    env_extra: dict[str, str] | None = None  # 注入子进程的额外环境变量(如 OPENBLAS_NUM_THREADS=1)
    profile: str | None = None
    allowed_domains: list[str] | None = None  # srt 专用: network_blocked=False 时的域名白名单; None 则 srt 模式下拒绝执行 (fail-closed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_limit": self.memory_limit,
            "cpu_limit": self.cpu_limit,
            "time_limit": self.time_limit,
            "disk_limit": self.disk_limit,
            "network_blocked": self.network_blocked,
            "working_dir": self.working_dir,
            "allow_write": self.allow_write,
            "audit_enabled": self.audit_enabled,
            "env_extra": self.env_extra,
            "allowed_domains": self.allowed_domains,
            "profile": self.profile,
        }


class SandboxProfile(StrEnum):
    """显式资源与 capability profile。

    ``profile_config`` 是唯一的配置工厂；``profile_for`` 只负责从已有配置
    反推标签。TEST 与 BUILD 当前共享 ``allow_write=True`` 的 capability 边界，
    但 TEST 使用更短时限，调用方仍可在测试执行后丢弃工作区。
    """

    READ_ONLY = "read_only"
    TEST = "test"
    BUILD = "build"
    NETWORKED = "networked"
    PRIVILEGED = "privileged"


def profile_for(config: SandboxConfig) -> SandboxProfile:
    """从 allow_write/network_blocked 推导出人可读的 profile 标签(只读, 不改判断)。"""
    if config.profile:
        try:
            return SandboxProfile(config.profile)
        except ValueError:
            pass
    if config.allow_write and not config.network_blocked:
        return SandboxProfile.PRIVILEGED
    if not config.network_blocked:
        return SandboxProfile.NETWORKED
    if config.allow_write:
        return SandboxProfile.BUILD
    return SandboxProfile.READ_ONLY


def profile_config(
    profile: SandboxProfile | str,
    *,
    working_dir: str | None = None,
) -> SandboxConfig:
    """Build one explicit resource/capability policy for a sandbox profile."""
    selected = profile if isinstance(profile, SandboxProfile) else SandboxProfile(profile)
    if selected is SandboxProfile.READ_ONLY:
        return SandboxConfig(
            memory_limit=256 * 1024 * 1024,
            cpu_limit=30,
            time_limit=60,
            network_blocked=True,
            allow_write=False,
            working_dir=working_dir,
            audit_enabled=True,
            reject_dangerous=True,
            profile=selected.value,
        )
    if selected is SandboxProfile.TEST:
        return SandboxConfig(
            memory_limit=512 * 1024 * 1024,
            cpu_limit=120,
            time_limit=300,
            network_blocked=True,
            allow_write=True,
            working_dir=working_dir,
            audit_enabled=True,
            reject_dangerous=True,
            profile=selected.value,
        )
    if selected is SandboxProfile.BUILD:
        return SandboxConfig(
            memory_limit=512 * 1024 * 1024,
            cpu_limit=120,
            time_limit=600,
            network_blocked=True,
            allow_write=True,
            working_dir=working_dir,
            audit_enabled=True,
            reject_dangerous=True,
            profile=selected.value,
        )
    if selected is SandboxProfile.NETWORKED:
        return SandboxConfig(
            memory_limit=512 * 1024 * 1024,
            cpu_limit=120,
            time_limit=600,
            network_blocked=False,
            allow_write=False,
            working_dir=working_dir,
            audit_enabled=True,
            reject_dangerous=True,
            profile=selected.value,
        )
    return SandboxConfig(
        memory_limit=1024 * 1024 * 1024,
        cpu_limit=300,
        time_limit=900,
        network_blocked=False,
        allow_write=True,
        working_dir=working_dir,
        audit_enabled=True,
        reject_dangerous=True,
        profile=selected.value,
    )


class Sandbox(ABC):
    """
    Sandbox abstract base class.

    Features:
    1. Resource limiting and monitoring
    2. Operation audit
    3. Automatic rollback
    4. Execution environment isolation
    """

    def __init__(self, config: SandboxConfig):
        self.config = config
        self.status = SandboxStatus.IDLE
        self.start_time: float | None = None
        self.end_time: float | None = None
        self.audit_log: list[AuditLog] = []
        self.resource_usage: dict[ResourceType, float] = defaultdict(float)
        self.temp_dir: str | None = None
        self.original_cwd = os.getcwd()
        self._saved_rlimits: dict[int, tuple[int, int]] = {}
        self.logger = logging.getLogger(f"sandbox.{id(self)}")
        self._srt_settings_path: str | None = None
        self._last_backend: str = "legacy"


        # 确保审计日志目录存在 (cwd 不可写时 — 如容器 /app 只读 — fallback 到系统临时目录)
        if self.config.audit_enabled:
            self.audit_dir = Path(".veya/audit")
            try:
                self.audit_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.audit_dir = Path(tempfile.gettempdir()) / "veya_audit"
                self.audit_dir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    async def execute(self, command: str, **kwargs: Any) -> dict[str, Any]:
        """Execute a command."""
        pass

    @abstractmethod
    async def execute_args(self, argv: list[str], **kwargs: Any) -> dict[str, Any]:
        """Execute an argv array (no shell injection surface, G4)."""
        pass

    @abstractmethod
    async def run_script(self, script: str, **kwargs: Any) -> dict[str, Any]:
        """Run a script."""
        pass

    @abstractmethod
    async def cancel(self) -> None:
        """Cancel execution."""
        pass

    def setup_environment(self) -> None:
        """Set up the execution environment."""
        # 创建临时工作目录
        self.temp_dir = tempfile.mkdtemp(prefix="veya_sandbox_")
        if self.config.working_dir:
            os.makedirs(os.path.join(self.temp_dir, self.config.working_dir), exist_ok=True)

        # 应用资源限制
        self._apply_resource_limits()

        # 记录开始日志
        self._audit_log("sandbox_start", "Sandbox started", status="started")

    def cleanup_environment(self) -> None:
        """Tear down the execution environment."""
        # 清理临时目录
        if self.temp_dir and os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
            except Exception as e:
                self.logger.error(f"Failed to clean up sandbox: {e}")
        self.temp_dir = None
        # 清理 srt settings 临时文件 (600, 仅本次执行用)
        settings_path = getattr(self, "_srt_settings_path", None)
        if settings_path and os.path.exists(settings_path):
            with contextlib.suppress(OSError):
                os.unlink(settings_path)
        self._srt_settings_path = None

        # 恢复原始工作目录
        os.chdir(self.original_cwd)

        # 恢复被 _apply_resource_limits 降低的进程级资源限制
        self._restore_resource_limits()

        # 记录结束日志
        self._audit_log("sandbox_end", "Sandbox ended", status="completed")

    def _apply_resource_limits(self) -> None:
        """
        Apply per-command resource limits WITHOUT touching the host process.

        The host process must never have its own rlimits lowered: RLIMIT_AS is
        inherited by every child via fork, and a lowered soft limit starves the
        host's own allocations (imports, coverage instrumentation, pytest) —
        previously this caused MemoryError / 0%-coverage once any memory-capped
        sandbox run happened in the same process.

        Limits are instead emitted as a POSIX shell prefix (``ulimit -v …; exec …``)
        prepended to the command in :meth:`_limit_prefix`, so they apply only to
        the sandboxed child and its descendants.

        CPU % / disk quotas require cgroups / filesystem quotas on Linux;
        implementing them here is intentionally a documented no-op.
        """
        # Host process rlimits are deliberately left untouched.
        return None

    def _limit_prefix(self) -> str:
        """Return a POSIX shell prefix enforcing memory/CPU limits on the child only."""
        parts: list[str] = []
        if self.config.memory_limit:
            # RLIMIT_AS is measured in KiB for `ulimit -v`.
            parts.append(f"ulimit -v {max(1, self.config.memory_limit // 1024)} 2>/dev/null;")
        if self.config.cpu_limit:
            # RLIMIT_CPU is measured in whole seconds for `ulimit -t`.
            parts.append(f"ulimit -t {max(1, int(self.config.cpu_limit))} 2>/dev/null;")
        return " ".join(parts)

    def _write_srt_settings(self) -> str:
        """把本次执行的 srt settings 写进系统临时目录的 600 文件, 返回路径。

        srt 在沙箱外读 settings, 故不放工作目录: 工作目录在 allowWrite 内,
        子进程理论上可改写它 (读时已定, 但没必要留这个口子)。
        """
        assert self.temp_dir is not None
        settings = build_srt_settings(self.config, work_dir=self.temp_dir)
        fd, path = tempfile.mkstemp(prefix="veya_srt_", suffix=".json")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(settings, handle)
            os.chmod(path, 0o600)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise
        self._srt_settings_path = path
        return path

    def _isolation_argv(self) -> list[str]:
        """隔离前缀: srt 模式走 srt, 否则旧 bwrap 路径。

        srt 形态: [srt, --settings, <file>, --] + 后续 bash/argv; srt 接管
        后, 原 bash 命令原样成为被包装命令 (argv 透传与 -c 形式均已实测)。
        """
        if _srt_enabled_for(self.config):
            self._last_backend = "srt"
            binary = _srt_binary()
            if binary is None:
                raise RuntimeError(
                    "VEYA_SANDBOX_BACKEND=srt but no srt binary found "
                    "(set VEYA_SRT_BIN or install @anthropic-ai/sandbox-runtime)"
                )
            settings_path = self._write_srt_settings()
            return [binary, "--settings", settings_path, "--"]
        self._last_backend = "legacy"
        return self._legacy_isolation_argv()

    def _legacy_isolation_argv(self) -> list[str]:
        """旧 bubblewrap 路径: 仅 network_blocked 时加 bwrap --unshare-net。

        ``network_blocked`` used to be only proxy-env scrubbing, which a direct
        socket bypasses. When ``bwrap`` is present and ``network_blocked`` is set,
        wrap the child in ``bwrap --unshare-net`` so it has no network namespace
        at all. Returns the argv prefix (empty when unavailable/disabled). Set
        ``VEYA_SANDBOX_BWRAP=0`` to disable.
        """
        if not self.config.network_blocked:
            return []
        if str(os.environ.get("VEYA_SANDBOX_BWRAP", "1")).strip().lower() in {"0", "false", "no"}:
            return []
        if not _bwrap_available():
            return []
        # --bind / / keeps the filesystem view identical (write semantics
        # unchanged); the isolation this adds is the network namespace.
        return ["bwrap", "--die-with-parent", "--unshare-net", "--bind", "/", "/", "--"]

    def _restore_resource_limits(self) -> None:
        """No-op: host rlimits are never lowered (see _apply_resource_limits)."""
        self._saved_rlimits.clear()

    def _audit_log(
        self, action: str, resource: str, status: str = "success", **details: Any
    ) -> None:
        """Record an audit log entry."""
        if not self.config.audit_enabled:
            return

        log = AuditLog(
            action=action,
            resource=resource,
            status=status,
            details=details,
            session_id=details.get("session_id", ""),
            user=details.get("user", "system"),
        )
        self.audit_log.append(log)

        # 保存到文件
        if len(self.audit_log) % 10 == 0:  # 每10条记录写入文件
            self._save_audit_log()

    def _save_audit_log(self) -> None:
        """Persist the audit log to disk."""
        if not self.config.audit_enabled or not self.audit_log:
            return

        timestamp = int(time.time() * 1000)
        filename = self.audit_dir / f"audit_{timestamp}.json"
        with open(filename, "w") as f:
            json.dump([log.to_dict() for log in self.audit_log], f, indent=2)

    def get_audit_report(self) -> dict[str, Any]:
        """Get the audit report."""
        log = self.audit_log
        return {
            "total_entries": len(log),
            "actions": {
                a: len([entry for entry in log if entry.action == a])
                for a in set(e.action for e in log)
            },
            "status_distribution": {
                s: len([entry for entry in log if entry.status == s])
                for s in set(e.status for e in log)
            },
            "resource_usage": self.resource_usage,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration": (self.end_time - self.start_time)
            if self.start_time and self.end_time
            else None,
        }

    def get_resource_usage(self) -> dict[str, Any]:
        """Get resource usage."""
        return {
            "memory": self.resource_usage[ResourceType.MEMORY],
            "cpu": self.resource_usage[ResourceType.CPU],
            "disk": self.resource_usage[ResourceType.DISK],
            "time": self.resource_usage[ResourceType.TIME],
        }


# 进程沙箱实现


class ProcessSandbox(Sandbox):
    """
    Process sandbox implementation.

    Executes commands in a subprocess with basic isolation and resource limits.
    """

    def __init__(self, config: SandboxConfig):
        super().__init__(config)
        self.process: asyncio.subprocess.Process | None = None
        self.timeout_task: asyncio.Task | None = None

    async def execute(self, command: str, **kwargs: Any) -> dict[str, Any]:
        """Execute a command (shell semantics, backward compatible; dangerous commands are rejected first).

        Note: string commands go through the shell (pipelines/``&&`` supported) and are
        only meant for trusted internal calls; for untrusted input use
        :meth:`execute_args` (argv array, no shell injection surface).
        """
        if self.config.reject_dangerous and is_dangerous_command(command):
            return self._rejected_result(command)
        return await self._run_process(command, use_shell=True)

    async def execute_args(self, argv: list[str], **kwargs: Any) -> dict[str, Any]:
        """Execute an argv array: user arguments pass through verbatim, never shell-parsed (G4 anti-injection).

        Resource limits are enforced via a fixed wrapper (``bash -c '…ulimit…; exec "$@"'``);
        the wrapper is trusted and never contains user input.
        """
        if not argv:
            return {
                "exit_code": -2,
                "stdout": "",
                "stderr": "empty argv",
                "command": "",
                "duration": 0.0,
            }
        if self.config.reject_dangerous and is_dangerous_argv(argv):
            return self._rejected_result(" ".join(shlex.quote(a) for a in argv))
        return await self._run_process(argv, use_shell=False)

    def _rejected_result(self, command: str) -> dict[str, Any]:
        self._audit_log(
            "command_rejected",
            command,
            status="failed",
            reason="dangerous command blocked before execution",
        )
        return {
            "exit_code": -3,
            "stdout": "",
            "stderr": "Command rejected: potentially dangerous (blocked before execution)",
            "command": command,
            "duration": 0.0,
        }

    async def _run_process(self, command: str | list[str], *, use_shell: bool) -> dict[str, Any]:
        """Shared execution core: unified timeout/output/audit handling for both shell and argv forms."""
        display = command if isinstance(command, str) else " ".join(shlex.quote(a) for a in command)
        self.status = SandboxStatus.RUNNING
        self.start_time = time.time()

        try:
            self.setup_environment()

            # 资源限制只作用于子进程（POSIX ulimit 前缀/固定 wrapper），宿主不受影响
            prefix = self._limit_prefix()
            # SEC H5: 真网络隔离 (bwrap --unshare-net)。network_blocked 不再是
            # 仅清代理 env 的"软阻断"。
            isolation = self._isolation_argv()
            if use_shell:
                assert isinstance(command, str)
                full_command = f"{prefix} {command}" if prefix else command
                argv: list[str] = [*isolation, "bash", "-c", full_command]
                process: asyncio.subprocess.Process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=self.temp_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._prepare_env(),
                    start_new_session=True,
                )
            else:
                # 固定可信 wrapper：ulimit 前缀 + exec "$@"；用户参数原样传递
                inner: list[str] = [
                    "bash",
                    "-c",
                    f'{prefix} exec "$@"' if prefix else 'exec "$@"',
                    "veya-sandbox",
                ]
                if isinstance(command, list):
                    inner.extend(command)
                process = await asyncio.create_subprocess_exec(
                    *isolation,
                    *inner,
                    cwd=self.temp_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._prepare_env(),
                    start_new_session=True,
                )
            self.process = process

            # 设置超时任务
            if self.config.time_limit:
                self.timeout_task = asyncio.create_task(self._timeout_handler())

            # 读取输出
            stdout, stderr = await process.communicate()

            # 检查超时任务
            if self.timeout_task and not self.timeout_task.done():
                self.timeout_task.cancel()

            # 处理结果
            result = {
                "exit_code": process.returncode,
                "stdout": stdout.decode().strip(),
                "stderr": stderr.decode().strip(),
                "command": display,
                "duration": time.time() - self.start_time,
                "sandbox_backend": self._last_backend,
            }

            # 记录审计日志
            self._audit_log(
                "command_execute",
                display,
                status="success" if process.returncode == 0 else "failed",
                exit_code=process.returncode,
                duration=result["duration"],
                sandbox_backend=self._last_backend,
            )

            return result
        except TimeoutError:
            if self.process:
                self.process.terminate()
                await self.process.wait()
            self._audit_log("command_timeout", display, status="failed", reason="timeout")
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": "Command timed out",
                "command": display,
                "duration": time.time() - self.start_time,
            }
        except Exception as e:
            self._audit_log("command_failed", display, status="failed", error=str(e))
            return {
                "exit_code": -2,
                "stdout": "",
                "stderr": str(e),
                "command": display,
                "duration": time.time() - self.start_time,
            }
        finally:
            self.end_time = time.time()
            self.status = SandboxStatus.COMPLETED
            self.cleanup_environment()

    async def run_script(self, script: str, **kwargs: Any) -> dict[str, Any]:
        """Run a script (via argv execution, no shell concatenation)."""
        # Ensure environment is set up before writing the script file
        if not self.temp_dir:
            self.setup_environment()
        assert self.temp_dir is not None

        # 创建临时脚本文件
        script_path = os.path.join(self.temp_dir, "script.tmp")
        with open(script_path, "w") as f:
            f.write(script)

        # 执行脚本（参数数组形态）
        return await self.execute_args([sys.executable, script_path])

    async def cancel(self) -> None:
        """Cancel execution."""
        if self.process and self.process.returncode is None:
            try:
                pgid = os.getpgid(self.process.pid)
                os.killpg(pgid, signal.SIGTERM)
            except Exception:
                self.process.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.process.wait(), timeout=2.0)
            if self.process.returncode is None:
                with contextlib.suppress(Exception):
                    pgid = os.getpgid(self.process.pid)
                    os.killpg(pgid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    self.process.kill()
                await self.process.wait()
            self.status = SandboxStatus.CANCELLED
            self._audit_log(
                "execution_cancelled", "User requested cancellation", status="cancelled"
            )

    async def _timeout_handler(self) -> None:
        """Timeout handler."""
        assert self.config.time_limit is not None
        await asyncio.sleep(self.config.time_limit)
        if self.process and self.process.returncode is None:
            with contextlib.suppress(Exception):
                pgid = os.getpgid(self.process.pid)
                os.killpg(pgid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                self.process.kill()
            self._audit_log("execution_timeout", "Time limit exceeded", status="failed")

    def _prepare_env(self) -> dict[str, str]:
        """Prepare environment variables — allowlist only (SEC H5).

        旧实现 ``os.environ.copy()`` 把服务端全部环境变量 (含所有 provider
        API key、VEYA_* 配置) 复制进子进程; 一个被提示注入的 agent 或匿名调用者
        ``env`` 一下就能拿走密钥 (审计 C2/H5)。改为白名单: 只透传子进程正常运行
        必需的非敏感变量, 其余 (密钥/配置) 一律不进入子进程。
        """
        env: dict[str, str] = {}
        for name in _SANDBOX_ENV_ALLOWLIST:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        # 前缀式白名单 (LANG/LC_*/TZ 等无害本地化变量)
        for name, value in os.environ.items():
            if name.startswith(_SANDBOX_ENV_ALLOWLIST_PREFIXES):
                env.setdefault(name, value)
        if self.config.env_extra:
            # 注入额外环境(如 OPENBLAS_NUM_THREADS=1 规避 RLIMIT_AS 下 OpenBLAS 启动失败)
            env.update(self.config.env_extra)
        if getattr(self, "_last_backend", "legacy") == "srt":
            # srt 自己给子进程注入指向本地代理的 proxy env (含 per-run token),
            # 宿主的 egress 代理只应供给 srt 自身的上游, 不能原样进子进程;
            # srt 会覆盖子进程的 proxy 变量, 这里把宿主代理透传给 srt 进程即可。
            for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "no_proxy"):
                value = os.environ.get(name)
                if value is not None:
                    env[name] = value
            # 沙箱内临时文件统一落工作目录 (allowWrite 内), 避免 /tmp 绝对路径
            # 在 read-only 档被拒; 工作目录随沙箱删除, 不留宿主残留。
            if self.temp_dir:
                env["TMPDIR"] = self.temp_dir
                env["TMP"] = self.temp_dir
                env["TEMP"] = self.temp_dir
                # srt 硬编码子进程 TMPDIR = $CLAUDE_CODE_TMPDIR || '/tmp/claude'
                # (为 Claude Code 而生); veya 侧必须把它指到本次工作目录,
                # 否则子进程临时文件落到不存在的 /tmp/claude 直接失败。
                env["CLAUDE_CODE_TMPDIR"] = self.temp_dir
            return env
        if self.config.network_blocked:
            # 代理层软阻断 (纵深防御之一; 真隔离见 _isolation_argv 的 --unshare-net)
            env["NO_PROXY"] = "*"
            env["http_proxy"] = ""
            env["https_proxy"] = ""
        return env


# 文件系统沙箱实现


class FileSystemSandbox(Sandbox):
    """
    File-system sandbox.

    Provides rollback via file-system snapshots.
    """

    def __init__(self, config: SandboxConfig):
        super().__init__(config)
        self.snapshot_path: str | None = None
        self.original_files: dict[str, bytes] = {}

    def setup_environment(self) -> None:
        super().setup_environment()
        assert self.temp_dir is not None

        # 创建文件系统快照
        self.snapshot_path = os.path.join(self.temp_dir, "snapshot")
        os.makedirs(self.snapshot_path, exist_ok=True)

        # 复制工作目录内容（如果指定）
        if self.config.working_dir:
            source = self.config.working_dir
            target = os.path.join(self.temp_dir, os.path.basename(source))
            if os.path.exists(source):
                shutil.copytree(source, target)

                # 保存原始文件内容（用于回滚）
                for root, _, files in os.walk(target):
                    for file in files:
                        path = os.path.join(root, file)
                        rel_path = os.path.relpath(path, target)
                        with open(path, "rb") as f:
                            self.original_files[rel_path] = f.read()

    async def execute(self, command: str, **kwargs: Any) -> dict[str, Any]:
        """Execute a command."""
        self.status = SandboxStatus.RUNNING
        self.start_time = time.time()

        try:
            self.setup_environment()

            # 创建子沙箱执行命令
            sandbox = ProcessSandbox(self.config)
            result = await sandbox.execute(command, **kwargs)

            # 保存审计日志
            self._audit_log(
                "command_execute",
                command,
                status="success" if result["exit_code"] == 0 else "failed",
                exit_code=result["exit_code"],
                duration=result["duration"],
            )

            return result
        except Exception as e:
            self._audit_log("command_failed", command, status="failed", error=str(e))
            raise
        finally:
            self.end_time = time.time()
            self.status = SandboxStatus.COMPLETED

    async def run_script(self, script: str, **kwargs: Any) -> dict[str, Any]:
        """Run a script."""
        # Ensure environment is set up before writing the script file
        if not self.temp_dir:
            self.setup_environment()
        assert self.temp_dir is not None

        # 创建临时脚本文件
        script_path = os.path.join(self.temp_dir, "script.tmp")
        with open(script_path, "w") as f:
            f.write(script)

        # 执行脚本
        return await self.execute(f"{sys.executable} {script_path}", **kwargs)

    async def execute_args(self, argv: list[str], **kwargs: Any) -> dict[str, Any]:
        """Execute an argv array (delegates to ProcessSandbox.execute_args, no shell concatenation)."""
        self.status = SandboxStatus.RUNNING
        self.start_time = time.time()
        try:
            self.setup_environment()
            sandbox = ProcessSandbox(self.config)
            result = await sandbox.execute_args(argv, **kwargs)
            self._audit_log(
                "command_execute",
                " ".join(argv),
                status="success" if result["exit_code"] == 0 else "failed",
                exit_code=result["exit_code"],
            )
            return result
        finally:
            self.end_time = time.time()
            self.status = SandboxStatus.COMPLETED

    async def cancel(self) -> None:
        """Cancel execution."""
        # 不需要额外处理
        pass

    async def rollback(self) -> bool:
        """Roll back to the initial state."""
        if self.status in [
            SandboxStatus.CANCELLED,
            SandboxStatus.FAILED,
            SandboxStatus.ROLLED_BACK,
        ]:
            return False

        try:
            # 恢复原始文件
            assert self.temp_dir is not None
            for rel_path, content in self.original_files.items():
                target_path = os.path.join(self.temp_dir, rel_path)
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with open(target_path, "wb") as f:
                    f.write(content)

            # 记录回滚日志
            self._audit_log(
                "rollback", "File system rolled back to initial state", status="success"
            )
            self.status = SandboxStatus.ROLLED_BACK
            return True
        except Exception as e:
            self._audit_log(
                "rollback_failed", "Failed to roll back file system", status="failed", error=str(e)
            )
            return False


# 安全执行器


class SafeExecutor:
    """
    Safe executor - executes code safely through a sandbox.

    Features:
    1. Picks an appropriate sandbox automatically
    2. Applies resource limits
    3. Audits operations
    4. Rolls back on failure
    5. Thread-pool isolation for CPU-bound tasks (G14)
    6. Per-task thread isolation with configurable pool size
    """

    def __init__(self, config: SandboxConfig | None = None, *, max_workers: int = 4):
        self.config = config or SandboxConfig()
        self.sandbox: Sandbox | None = None
        self.active = False
        self.max_workers = max_workers
        self._thread_pool: concurrent.futures.ThreadPoolExecutor | None = None

    async def __aenter__(self) -> SafeExecutor:
        """Context-manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context-manager exit."""
        await self.stop()

    async def start(self) -> None:
        """Start the executor and thread pool."""
        if self.active:
            return

        import concurrent.futures

        # 选择合适的沙箱实现: 远程 socket 配好且无 working_dir 时走宿主 srt;
        # 否则保持原有本地分支 (working_dir 的相对路径在两端解析不同)。
        if _remote_socket_path() is not None and not self.config.working_dir:
            self.sandbox = RemoteSandbox(self.config)
        elif self.config.allow_write:
            self.sandbox = FileSystemSandbox(self.config)
        else:
            self.sandbox = ProcessSandbox(self.config)

        # Start thread pool for CPU-bound isolation
        self._thread_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="veya-sandbox-",
        )

        self.active = True

    async def stop(self) -> None:
        """Stop the executor and thread pool."""
        if self.sandbox:
            clean = getattr(self.sandbox, "cleanup", None) or getattr(
                self.sandbox, "cleanup_environment", None
            )
            if clean:
                clean()

        if self._thread_pool:
            self._thread_pool.shutdown(wait=False, cancel_futures=True)
            self._thread_pool = None

        self.active = False

    async def run_in_thread(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        """Run a CPU-bound function in an isolated thread.

        Args:
            func: The function to run.
            *args: Positional arguments.
            **kwargs: Keyword arguments.

        Returns:
            The function's return value.
        """
        if not self._thread_pool:
            await self.start()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._thread_pool,
            lambda: func(*args, **kwargs),
        )

    async def execute(self, command: str, **kwargs: Any) -> dict[str, Any]:
        """Execute a command (shell semantics, compatible)."""
        if not self.active:
            await self.start()

        if not self.sandbox:
            raise RuntimeError("Sandbox not initialized")

        return await self.sandbox.execute(command, **kwargs)

    async def execute_args(self, argv: list[str], **kwargs: Any) -> dict[str, Any]:
        """Execute an argv array (no shell injection surface; G4 recommended path)."""
        if not self.active:
            await self.start()

        if not self.sandbox:
            raise RuntimeError("Sandbox not initialized")

        return await self.sandbox.execute_args(argv, **kwargs)

    async def run_script(self, script: str, **kwargs: Any) -> dict[str, Any]:
        """Run a script."""
        if not self.active:
            await self.start()

        if not self.sandbox:
            raise RuntimeError("Sandbox not initialized")

        return await self.sandbox.run_script(script, **kwargs)

    async def cancel(self) -> None:
        """Cancel execution."""
        if self.sandbox:
            await self.sandbox.cancel()

    def get_audit_report(self) -> dict[str, Any]:
        """Get the audit report."""
        if self.sandbox:
            return self.sandbox.get_audit_report()
        return {}

    def get_resource_usage(self) -> dict[str, Any]:
        """Get resource usage."""
        if self.sandbox:
            return self.sandbox.get_resource_usage()
        return {}


# 高级安全工具


class SecureTool:
    """
    Secure tool wrapper.

    Adds safe sandbox execution to existing tools.
    """

    def __init__(self, tool: Any, config: SandboxConfig | None = None):
        self.tool = tool
        self.config = config or SandboxConfig()
        self.executor = SafeExecutor(self.config)

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Execute a tool safely."""
        # 准备命令
        command = self._prepare_command(**kwargs)

        async with self.executor:
            # 执行命令
            result = await self.executor.execute(command)

            # 解析输出
            if result["exit_code"] == 0:
                return self._parse_output(result["stdout"])
            else:
                return {
                    "status": "failed",
                    "error": result["stderr"],
                    "exit_code": result["exit_code"],
                }

    def _prepare_command(self, **kwargs: Any) -> str:
        """Prepare the command to execute."""
        # 具体实现取决于工具类型
        if hasattr(self.tool, "metadata") and hasattr(self.tool.metadata, "name"):
            tool_name = self.tool.metadata.name
            if tool_name == "git":
                return f"git {kwargs.get('command', '')}"
            elif tool_name == "terminal":
                value = kwargs.get("command", "")
                return str(value)
        return str(kwargs)

    def _parse_output(self, output: str) -> dict[str, Any]:
        """Parse the output."""
        return {"status": "success", "output": output}


# 便捷函数

# ── 远程沙箱 (宿主侧 srt 执行服务客户端) ──────────────────────────────
# 背景: 生产容器内 bwrap 建不了 namespace, srt 无法在容器内运行。宿主机上
# 跑一个守护进程 (server/sandbox_remote_daemon.py, systemd 用户服务), 它在
# 宿主侧用 srt 执行沙箱命令; 容器通过只读挂载的 socket 目录连过去。
# 环境变量 VEYA_SANDBOX_REMOTE_SOCKET 设为容器内 socket 路径即启用。
#
# 信任边界: 守护进程只跑 srt 沙箱命令, 不跑裸命令, 所以拿到 socket 的只能
# 是沙箱执行, 不是宿主 shell。socket 目录在宿主侧位于 ~/.veya 下, srt 的
# denyRead 会把它整个掩盖, 沙箱内的子进程连不回守护进程 (防逃逸)。
# 远程只在 config.working_dir 为空时启用: working_dir 的相对路径在容器和
# 宿主侧解析结果不同, 有 working_dir 的调用保持本地执行。

_REMOTE_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_REMOTE_MAX_RESPONSE_BYTES = 32 * 1024 * 1024


def _remote_socket_path() -> str | None:
    path = os.environ.get("VEYA_SANDBOX_REMOTE_SOCKET", "").strip()
    return path or None


class RemoteConnectError(Exception):
    """Socket connect failed before the request was sent (safe to fall back)."""


class RemoteProtocolError(Exception):
    """Request sent but the round-trip failed (do NOT retry: may double-run)."""


def _remote_recvall(sock: Any, n: int) -> bytes:
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _remote_call(socket_path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """One JSON request/response over the daemon Unix socket (blocking)."""
    import socket as _socket
    import struct as _struct

    raw = json.dumps(payload).encode("utf-8")
    if len(raw) > _REMOTE_MAX_REQUEST_BYTES:
        raise RemoteProtocolError("request too large")
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(socket_path)
    except OSError as exc:
        raise RemoteConnectError(str(exc)) from exc
    try:
        sock.sendall(_struct.pack(">I", len(raw)) + raw)
        header = _remote_recvall(sock, 4)
        if len(header) < 4:
            raise RemoteProtocolError("daemon closed connection")
        (n,) = _struct.unpack(">I", header)
        if n > _REMOTE_MAX_RESPONSE_BYTES:
            raise RemoteProtocolError("response too large")
        data = _remote_recvall(sock, n)
    except RemoteProtocolError:
        raise
    except OSError as exc:
        raise RemoteProtocolError(str(exc)) from exc
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    resp = json.loads(data.decode("utf-8"))
    if "error" in resp:
        raise RemoteProtocolError(str(resp["error"])[:500])
    return resp


class RemoteSandbox(Sandbox):
    """Sandbox whose execution happens in the host daemon (srt on host).

    Same result shape as ProcessSandbox. If the daemon socket cannot be
    reached at connect time (daemon down, socket not mounted), falls back
    to a local ProcessSandbox and marks the result, so a daemon outage
    degrades to today's behavior instead of breaking tool execution.
    """

    def __init__(self, config: SandboxConfig):
        super().__init__(config)
        self._socket_path = _remote_socket_path()

    def setup_environment(self) -> None:
        pass  # temp dirs live on the daemon side

    def cleanup_environment(self) -> None:
        pass

    def cleanup(self) -> None:
        pass

    def _rejected_result(self, command: str) -> dict[str, Any]:
        return {
            "exit_code": -3,
            "stdout": "",
            "stderr": "Command rejected: potentially dangerous (blocked before execution)",
            "command": command,
            "duration": 0.0,
        }

    async def execute(self, command: str, **kwargs: Any) -> dict[str, Any]:
        if self.config.reject_dangerous and is_dangerous_command(command):
            return self._rejected_result(command)
        return await self._run_remote("execute", {"command": command})

    async def execute_args(self, argv: list[str], **kwargs: Any) -> dict[str, Any]:
        if not argv:
            return {
                "exit_code": -2,
                "stdout": "",
                "stderr": "empty argv",
                "command": "",
                "duration": 0.0,
            }
        if self.config.reject_dangerous and is_dangerous_argv(argv):
            return self._rejected_result(" ".join(shlex.quote(a) for a in argv))
        return await self._run_remote("execute_args", {"command": argv})

    async def run_script(self, script: str, **kwargs: Any) -> dict[str, Any]:
        return await self._run_remote("run_script", {"script": script})

    async def cancel(self) -> None:
        pass  # daemon enforces config.time_limit; no remote cancel protocol

    async def _run_remote(self, kind: str, fields: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "version": 1,
            "kind": kind,
            **fields,
            "config": self.config.to_dict(),
        }
        timeout = (self.config.time_limit or 300) + 120
        try:
            result = await asyncio.to_thread(
                _remote_call, self._socket_path or "", payload, timeout
            )
        except RemoteConnectError as exc:
            self.logger.warning(
                "sandbox remote unavailable (%s); falling back to local", exc
            )
            local = ProcessSandbox(self.config)
            if kind == "execute":
                result = await local.execute(fields["command"])
            elif kind == "execute_args":
                result = await local.execute_args(fields["command"])
            else:
                result = await local.run_script(fields["script"])
            result["sandbox_fallback"] = "remote-unavailable"
            return result
        except RemoteProtocolError as exc:
            return {
                "exit_code": -2,
                "stdout": "",
                "stderr": f"sandbox remote error: {exc}",
                "command": fields.get("command", ""),
                "duration": 0.0,
                "sandbox_backend": "remote-error",
            }
        result["sandbox_backend"] = "srt-remote"
        return result

def create_sandbox(config: SandboxConfig | None = None) -> Sandbox:
    """Create a sandbox instance."""
    if not config:
        config = SandboxConfig()
    if _remote_socket_path() is not None and not config.working_dir:
        return RemoteSandbox(config)
    return ProcessSandbox(config)


def create_safe_executor(config: SandboxConfig | None = None) -> SafeExecutor:
    """Create a safe executor."""
    return SafeExecutor(config)


if __name__ == "__main__":

    async def test_sandbox() -> None:
        print("=== Testing Process Sandbox ===")
        config = SandboxConfig(
            memory_limit=100 * 1024 * 1024,  # 100 MB
            cpu_limit=50.0,  # 50% CPU
            time_limit=5.0,  # 5 seconds
            network_blocked=True,
        )

        sandbox = ProcessSandbox(config)

        # 测试内存限制
        print("\n1. Testing memory limit...")
        try:
            result = await sandbox.execute(
                "python -c \"import sys; a = 'x' * 200000000; print(len(a))\"", session_id="test1"
            )
            print(f"Result: {result}")
        except Exception as e:
            print(f"Memory limit test failed: {e}")

        # 测试时间限制
        print("\n2. Testing time limit...")
        result = await sandbox.execute("sleep 10", session_id="test2")
        print(f"Result: {result}")

        # 获取审计报告
        print("\n3. Audit report:")
        report = sandbox.get_audit_report()
        print(json.dumps(report, indent=2))

    async def test_filesystem_sandbox() -> None:
        print("\n=== Testing File System Sandbox ===")
        config = SandboxConfig(working_dir=".", allow_write=True, audit_enabled=True)

        sandbox = FileSystemSandbox(config)

        # 创建测试文件
        test_file = "test.txt"
        with open(test_file, "w") as f:
            f.write("Original content")

        try:
            # 执行命令修改文件
            print("\n1. Modifying file...")
            result = await sandbox.execute(
                f"echo 'Modified content' > {test_file}", session_id="fs_test"
            )
            print(f"Result: {result}")

            # 回滚
            print("\n2. Rolling back...")
            success = await sandbox.rollback()
            print(f"Rollback successful: {success}")

            # 验证回滚
            with open(test_file) as f:
                content = f.read()
            print(f"File content after rollback: {content}")
        finally:
            # 清理测试文件
            if os.path.exists(test_file):
                os.remove(test_file)

        # 获取审计报告
        print("\n3. Audit report:")
        report = sandbox.get_audit_report()
        print(json.dumps(report, indent=2))

    async def test_safe_executor() -> None:
        print("\n=== Testing Safe Executor ===")
        config = SandboxConfig(memory_limit=50 * 1024 * 1024, time_limit=3.0, audit_enabled=True)

        executor = SafeExecutor(config)

        try:
            await executor.start()

            # 测试命令执行
            print("\n1. Executing command...")
            result = await executor.execute("ls -la", session_id="exec_test")
            print(f"Result: {result}")

            # 测试脚本执行
            print("\n2. Running script...")
            script = """
import time
print("Starting...")
time.sleep(2)
print("Done!")
"""
            result = await executor.run_script(script, session_id="exec_test")
            print(f"Script result: {result}")

            # 获取资源使用情况
            print("\n3. Resource usage:")
            usage = executor.get_resource_usage()
            print(json.dumps(usage, indent=2))
        finally:
            await executor.stop()

    # 运行测试
    asyncio.run(test_sandbox())
    asyncio.run(test_filesystem_sandbox())
    asyncio.run(test_safe_executor())
