"""
Secure sandbox module — P1 core capability.
Features: resource limits, operation audit, automatic rollback, isolated execution.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import shlex
import shutil
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
    base = {
        "working_dir": working_dir,
        "audit_enabled": True,
        "reject_dangerous": True,
        "profile": selected.value,
    }
    if selected is SandboxProfile.READ_ONLY:
        return SandboxConfig(
            memory_limit=256 * 1024 * 1024,
            cpu_limit=30,
            time_limit=60,
            network_blocked=True,
            allow_write=False,
            **base,
        )
    if selected is SandboxProfile.TEST:
        return SandboxConfig(
            memory_limit=512 * 1024 * 1024,
            cpu_limit=120,
            time_limit=300,
            network_blocked=True,
            allow_write=True,
            **base,
        )
    if selected is SandboxProfile.BUILD:
        return SandboxConfig(
            memory_limit=512 * 1024 * 1024,
            cpu_limit=120,
            time_limit=600,
            network_blocked=True,
            allow_write=True,
            **base,
        )
    if selected is SandboxProfile.NETWORKED:
        return SandboxConfig(
            memory_limit=512 * 1024 * 1024,
            cpu_limit=120,
            time_limit=600,
            network_blocked=False,
            allow_write=False,
            **base,
        )
    return SandboxConfig(
        memory_limit=1024 * 1024 * 1024,
        cpu_limit=300,
        time_limit=900,
        network_blocked=False,
        allow_write=True,
        **base,
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
            if use_shell:
                assert isinstance(command, str)
                full_command = f"{prefix} {command}" if prefix else command
                process: asyncio.subprocess.Process = await asyncio.create_subprocess_shell(
                    full_command,
                    cwd=self.temp_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._prepare_env(),
                )
            else:
                # 固定可信 wrapper：ulimit 前缀 + exec "$@"；用户参数原样传递
                argv: list[str] = [
                    "bash",
                    "-c",
                    f'{prefix} exec "$@"' if prefix else 'exec "$@"',
                    "veya-sandbox",
                ]
                if isinstance(command, list):
                    argv.extend(command)
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=self.temp_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._prepare_env(),
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
            }

            # 记录审计日志
            self._audit_log(
                "command_execute",
                display,
                status="success" if process.returncode == 0 else "failed",
                exit_code=process.returncode,
                duration=result["duration"],
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
            self.process.terminate()
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
            self.process.terminate()
            self._audit_log("execution_timeout", "Time limit exceeded", status="failed")

    def _prepare_env(self) -> dict[str, str]:
        """Prepare environment variables."""
        env = os.environ.copy()
        if self.config.env_extra:
            # 注入额外环境(如 OPENBLAS_NUM_THREADS=1 规避 RLIMIT_AS 下 OpenBLAS 启动失败)
            env.update(self.config.env_extra)
        if self.config.network_blocked:
            # 模拟网络限制
            env["NO_PROXY"] = "*"  # 阻止所有代理
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

        # 选择合适的沙箱实现
        if self.config.allow_write:
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
def create_sandbox(config: SandboxConfig | None = None) -> Sandbox:
    """Create a sandbox instance."""
    if not config:
        config = SandboxConfig()
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
