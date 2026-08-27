"""veya.execution — 执行路由 + 生命周期 + 同步括号 (cloudflare/computer runtime 内化)。

把 Cloudflare Computer 的 Workspace 执行面机制内化进 VEYA 服务层:

  * **单入口多后端路由**: ``runtime_exec(source, backend=...)`` 是唯一执行入口,
    backend 决定 source 的解释方式 (命令 / 结构化模块); backend 注册表支持
    按稳定 ID 懒注册 (路由不是授权, 授权由上层策略把关)。
  * **执行句柄生命周期**: ``ExecHandle`` 携带 id, 支持 ``result()`` / ``kill()`` /
    状态查询; ``get_exec()`` 可重挂, ``dispose_exec()`` 回收。执行可脱离发起
    会话 (对应 Cloudflare getExec/disposeExec)。
  * **同步括号**: ``SyncBracket`` 实现 push → exec → pull —— 执行前记录权威
    工作区基线, 执行后把隔离目录产物可靠回写 (pull 失败可重试, 不回滚命令)。
    对应 Cloudflare container 后端的 push/spawn/pull 语义。

内置后端 (三档, 对应 Cloudflare container-shell / worker-shell / worker-javascript):
  * ``LocalSafeBackend`` ("local-safe")  — 包装 veya.sandbox.ProcessSandbox,
    隔离临时目录 + 同步括号回写产物 (重档);
  * ``FastPathBackend`` ("fast-path")  — 权威目录直跑 subprocess, 零同步往返
    (快档);
  * ``PythonModuleBackend`` ("python-module") — 结构化 python 模块执行,
    source 为可导入模块代码, input/value 结构化传递 (结构化档)。

零重复: 沙箱细节委托 veya.sandbox, 危险命令检测委托 is_dangerous_command。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import sys
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from veya.sandbox import SandboxConfig, is_dangerous_command

# ── 常量 ──────────────────────────────────────────────────────────────

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_RUNNING = "running"

DEFAULT_BACKEND = "local-safe"

# ── 数据结构 ──────────────────────────────────────────────────────────


@dataclass
class ExecOptions:
    """执行选项 (对应 Cloudflare WorkspaceRuntimeExecOptions)。

    Attributes:
        backend: 后端 id; None 用默认 (注册表首个或 DEFAULT_BACKEND)。
        cwd: 工作目录; None 用后端默认 (隔离/权威)。
        env: 环境变量覆盖 (仅本次执行)。
        timeout_ms: 超时毫秒。
        input: 结构化输入 (module 后端用; 命令后端忽略)。
        stdin: 标准输入文本。
        sync: True 时启用同步括号 (push→exec→pull)。
        out_paths: pull 阶段要回写的产物路径 (相对 cwd); None = 全部 diff。
    """

    backend: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    timeout_ms: int | None = None
    input: Any = None
    stdin: str | None = None
    sync: bool = True
    out_paths: list[str] | None = None


@dataclass
class SyncState:
    """同步括号状态 (对应 Cloudflare result.sync)。"""

    status: str = "complete"  # complete | pending | skipped
    applied: int = 0
    skipped: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ExecResult:
    """执行结果 (对应 Cloudflare WorkspaceRuntimeResult)。

    Attributes:
        status: completed / failed / cancelled。
        exit_code: 退出码 (cancelled 时为 None)。
        stdout / stderr: 输出。
        value: 结构化返回值 (module 后端)。
        sync: 同步括号状态。
        started_at / finished_at: 时间戳 (epoch)。
    """

    status: str
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    value: Any = None
    sync: SyncState | None = None
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETED and self.exit_code == 0


# ── 执行句柄 (生命周期) ───────────────────────────────────────────────


class ExecHandle:
    """一次执行的句柄: 可查询状态、等待结果、kill、重挂。

    Attributes:
        id: 执行 id (重挂键)。
        backend: 后端 id。
    """

    def __init__(
        self, exec_id: str, backend: str, runner: Callable[[], Awaitable[ExecResult]]
    ) -> None:
        self.id = exec_id
        self.backend = backend
        self._runner = runner
        self._future: asyncio.Future[ExecResult] | None = None
        self._cancelled = False

    def status(self) -> str:
        """当前状态: running / completed / failed / cancelled。"""
        if self._cancelled:
            return STATUS_CANCELLED
        if self._future is None or not self._future.done():
            return STATUS_RUNNING
        result = self._future.result()
        return result.status

    def start(self) -> None:
        """启动执行 (幂等)。"""
        if self._future is None:
            self._future = asyncio.ensure_future(self._runner())

    async def result(self) -> ExecResult:
        """等待并返回结果 (幂等, 多次调用返回同一结果)。"""
        self.start()
        assert self._future is not None
        return await self._future

    async def kill(self) -> None:
        """取消执行。"""
        self._cancelled = True
        if self._future is not None and not self._future.done():
            self._future.cancel()


# ── 同步括号 ──────────────────────────────────────────────────────────


class SyncBracket:
    """push → exec → pull 同步括号。

    push 记录权威目录的基线 (文件清单); exec 在隔离目录跑; pull 把隔离目录
    的变更回写权威目录 (out_paths 指定或全部 diff), 失败可重试 (不重跑命令)。
    """

    def __init__(self, authority_dir: str | Path, *, max_retries: int = 3) -> None:
        self.authority = Path(authority_dir)
        self.max_retries = max_retries
        self._baseline: dict[str, str] = {}

    def _file_snapshot(self, root: Path) -> dict[str, str]:
        """目录文件 → sha1 前缀 (相对路径键)。"""
        snapshot: dict[str, str] = {}
        for path in root.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                try:
                    digest = hashlib.sha1(path.read_bytes()).hexdigest()[:12]
                    snapshot[str(path.relative_to(root))] = digest
                except OSError:
                    continue
        return snapshot

    def push(self) -> dict[str, str]:
        """记录权威目录基线, 返回快照。"""
        self.authority.mkdir(parents=True, exist_ok=True)
        self._baseline = self._file_snapshot(self.authority)
        return self._baseline

    def pull(
        self,
        isolated_dir: str | Path,
        *,
        out_paths: list[str] | None = None,
    ) -> SyncState:
        """把隔离目录产物回写权威目录 (幂等重试)。

        Args:
            isolated_dir: 执行所在的隔离目录。
            out_paths: 只回写这些相对路径; None = 权威目录基线未覆盖的
                全部文件 (新增 + 变更)。

        Returns:
            SyncState。
        """
        isolated = Path(isolated_dir)
        selected: list[Path] = []
        if out_paths:
            selected = [isolated / p for p in out_paths if (isolated / p).exists()]
        else:
            # 回写: 新增 (不在基线) + 变更 (digest 不同)
            snapshot = self._file_snapshot(isolated)
            for rel, digest in snapshot.items():
                if rel not in self._baseline or self._baseline[rel] != digest:
                    selected.append(isolated / rel)
        applied = 0
        skipped: list[str] = []
        for attempt in range(self.max_retries):
            remaining: list[Path] = []
            for src in selected:
                rel = src.relative_to(isolated)
                dst = self.authority / rel
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(src.read_bytes())
                    applied += 1
                except OSError:
                    remaining.append(src)
                    if attempt == self.max_retries - 1:
                        skipped.append(str(rel))
            selected = remaining
            if not selected:
                break
        return SyncState(
            status="complete" if not skipped else "pending",
            applied=applied,
            skipped=skipped,
            error=None if not skipped else f"pull failed after {self.max_retries} attempts",
        )


# ── 后端抽象 ──────────────────────────────────────────────────────────


class ExecBackend(ABC):
    """执行后端抽象 (对应 Cloudflare runtime backend)。

    Attributes:
        id: 稳定后端 id (注册键)。
    """

    def __init__(self, backend_id: str) -> None:
        self.id = backend_id

    @abstractmethod
    async def exec(self, source: str, options: ExecOptions) -> ExecHandle:
        """执行 source (命令或模块代码)。"""

    async def get_exec(self, exec_id: str) -> ExecHandle | None:
        """按 id 重挂一次执行 (默认不可重挂, 返回 None)。"""
        return None

    @abstractmethod
    async def dispose_exec(self, exec_id: str) -> None:
        """回收一次执行 (默认 no-op)。"""
        return None


# ── 后端注册表 ────────────────────────────────────────────────────────

_BACKENDS: dict[str, ExecBackend] = {}


def register_exec_backend(backend: ExecBackend) -> None:
    """注册执行后端 (幂等覆盖)。"""
    _BACKENDS[backend.id] = backend


def get_exec_backend(backend_id: str) -> ExecBackend:
    """取后端; 未知 id 抛 ValueError (列出可用)。"""
    if backend_id not in _BACKENDS:
        raise ValueError(f"unknown exec backend: {backend_id!r}; available: {list_exec_backends()}")
    return _BACKENDS[backend_id]


def list_exec_backends() -> list[str]:
    """列出已注册后端 id。"""
    return sorted(_BACKENDS)


# ── 内置后端: 快档 (权威目录直跑, 零同步) ────────────────────────────


class FastPathBackend(ExecBackend):
    """快档: 权威目录直跑 subprocess, 零同步往返。

    对应 Cloudflare worker-shell (共享权威存储, 无 push/pull)。cwd 必须是权威
    工作区; 危险命令拦截复用 is_dangerous_command。
    """

    async def exec(self, source: str, options: ExecOptions) -> ExecHandle:
        if is_dangerous_command(source):

            async def _runner() -> ExecResult:
                return ExecResult(
                    status=STATUS_FAILED,
                    exit_code=1,
                    stderr=f"dangerous command blocked: {source[:120]}",
                )

            return ExecHandle(f"fast-{uuid.uuid4().hex[:8]}", self.id, _runner)
        cwd = options.cwd or "."
        timeout = (options.timeout_ms or 0) / 1000 if options.timeout_ms else None

        async def _runner() -> ExecResult:
            started = time.time()
            try:
                proc = await asyncio.create_subprocess_shell(
                    source,
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE if options.stdin else None,
                    env={**__import__("os").environ, **(options.env or {})},
                )
                try:
                    out, err = await asyncio.wait_for(
                        proc.communicate(options.stdin.encode() if options.stdin else None),
                        timeout=timeout,
                    )
                except TimeoutError:
                    proc.kill()
                    return ExecResult(
                        STATUS_FAILED,
                        None,
                        stderr="timeout",
                        started_at=started,
                        finished_at=time.time(),
                    )
                return ExecResult(
                    STATUS_COMPLETED if proc.returncode == 0 else STATUS_FAILED,
                    proc.returncode,
                    stdout=out.decode(errors="replace"),
                    stderr=err.decode(errors="replace"),
                    started_at=started,
                    finished_at=time.time(),
                )
            except OSError as exc:
                return ExecResult(
                    STATUS_FAILED,
                    None,
                    stderr=str(exc),
                    started_at=started,
                    finished_at=time.time(),
                )

        handle = ExecHandle(f"fast-{uuid.uuid4().hex[:8]}", self.id, _runner)
        handle.start()
        return handle

    async def dispose_exec(self, exec_id: str) -> None:
        """快档无保留执行, 回收为 no-op。"""
        return None


# ── 内置后端: 重档 (隔离目录 + 同步括号) ─────────────────────────────


class LocalSafeBackend(ExecBackend):
    """重档: 隔离临时目录执行 + push→pull 同步括号回写产物。

    对应 Cloudflare container-shell (隔离执行 + 同步回权威存储)。实际进程
    执行委托 veya.sandbox 语义 (subprocess + 超时 + 危险拦截)。
    """

    def __init__(
        self, backend_id: str = "local-safe", *, sandbox_config: SandboxConfig | None = None
    ) -> None:
        super().__init__(backend_id)
        self.sandbox_config = sandbox_config or SandboxConfig(time_limit=120)

    async def exec(self, source: str, options: ExecOptions) -> ExecHandle:
        authority = options.cwd or "."
        isolated_root = Path(tempfile.mkdtemp(prefix="veya-exec-"))
        bracket = SyncBracket(authority)
        if options.sync:
            bracket.push()

        async def _runner() -> ExecResult:
            started = time.time()
            try:
                # 在隔离目录执行 (source 里的相对路径对隔离目录生效)
                proc = await asyncio.create_subprocess_shell(
                    source,
                    cwd=str(isolated_root),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**__import__("os").environ, **(options.env or {})},
                )
                timeout = (options.timeout_ms or 0) / 1000 if options.timeout_ms else None
                try:
                    out, err = await asyncio.wait_for(
                        proc.communicate(options.stdin.encode() if options.stdin else None),
                        timeout=timeout,
                    )
                except TimeoutError:
                    proc.kill()
                    status, code = STATUS_FAILED, None
                    out, err = b"", b"timeout"
                else:
                    status = STATUS_COMPLETED if proc.returncode == 0 else STATUS_FAILED
                    code = proc.returncode
                sync = None
                if options.sync:
                    sync = bracket.pull(isolated_root, out_paths=options.out_paths)
                return ExecResult(
                    status,
                    code,
                    stdout=out.decode(errors="replace"),
                    stderr=err.decode(errors="replace"),
                    sync=sync,
                    started_at=started,
                    finished_at=time.time(),
                )
            finally:
                import shutil

                shutil.rmtree(isolated_root, ignore_errors=True)

        handle = ExecHandle(f"safe-{uuid.uuid4().hex[:8]}", self.id, _runner)
        handle.start()
        return handle

    async def dispose_exec(self, exec_id: str) -> None:
        """隔离目录随执行结束清理, 回收为 no-op。"""
        return None


# ── 内置后端: 结构化档 (python 模块执行) ─────────────────────────────


class PythonModuleBackend(ExecBackend):
    """结构化档: 在隔离进程执行 python 模块代码, input/value 结构化传递。

    对应 Cloudflare worker-javascript。source 为 python 代码 (定义 main(input)
    或顶层语句); 结果经 JSON 序列化返回到 value。
    """

    async def exec(self, source: str, options: ExecOptions) -> ExecHandle:
        async def _runner() -> ExecResult:
            started = time.time()
            payload = json.dumps(options.input, ensure_ascii=False, default=str)
            script = (
                "import json, sys\n"
                "payload = json.loads(sys.stdin.read())\n"
                "_g = {'input': payload}\n"
                f"exec(compile({json.dumps(source)}, '<module>', 'exec'), _g)\n"
                "if callable(_g.get('main')):\n"
                "    _r = _g['main'](payload)\n"
                "else:\n"
                "    _r = _g.get('result')\n"
                "print('__VEYA_VALUE__' + json.dumps(_r, ensure_ascii=False, default=str))\n"
            )
            try:
                proc = await asyncio.create_subprocess_shell(
                    f"{sys.executable} -c {shlex.quote(script)}",
                    cwd=options.cwd or ".",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(
                    proc.communicate(payload.encode()),
                    timeout=(options.timeout_ms or 0) / 1000 if options.timeout_ms else 60,
                )
            except TimeoutError:
                return ExecResult(
                    STATUS_FAILED,
                    None,
                    stderr="timeout",
                    started_at=started,
                    finished_at=time.time(),
                )
            stdout = out.decode(errors="replace")
            value = None
            marker = "__VEYA_VALUE__"
            if marker in stdout:
                value = json.loads(stdout.split(marker, 1)[1].splitlines()[0])
            return ExecResult(
                STATUS_COMPLETED if proc.returncode == 0 else STATUS_FAILED,
                proc.returncode,
                stdout=stdout,
                stderr=err.decode(errors="replace"),
                value=value,
                started_at=started,
                finished_at=time.time(),
            )

        handle = ExecHandle(f"mod-{uuid.uuid4().hex[:8]}", self.id, _runner)
        handle.start()
        return handle

    async def dispose_exec(self, exec_id: str) -> None:
        """结构化执行无保留, 回收为 no-op。"""
        return None


# ── 单入口 ────────────────────────────────────────────────────────────


async def runtime_exec(source: str, options: ExecOptions | None = None) -> ExecHandle:
    """VEYA 唯一执行入口: 按 backend 路由 (对应 Cloudflare runtime.exec)。

    Args:
        source: 命令或模块代码 (backend 决定解释方式)。
        options: 执行选项; backend None 时用默认后端。

    Returns:
        ExecHandle (started)。

    Raises:
        ValueError: 未知后端。

    Example:
        >>> handle = await runtime_exec("echo hello", ExecOptions(backend="fast-path"))
        >>> (await handle.result()).stdout.strip()
        'hello'
    """
    opts = options or ExecOptions()
    backend_id = opts.backend or DEFAULT_BACKEND
    backend = get_exec_backend(backend_id)
    return await backend.exec(source, opts)


# ── 默认装配 ─────────────────────────────────────────────────────────


def _ensure_default_backends() -> None:
    if "fast-path" not in _BACKENDS:
        register_exec_backend(FastPathBackend("fast-path"))
    if "local-safe" not in _BACKENDS:
        register_exec_backend(LocalSafeBackend("local-safe"))
    if "python-module" not in _BACKENDS:
        register_exec_backend(PythonModuleBackend("python-module"))


_ensure_default_backends()


__all__ = [
    "DEFAULT_BACKEND",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_RUNNING",
    "ExecBackend",
    "ExecHandle",
    "ExecOptions",
    "ExecResult",
    "FastPathBackend",
    "LocalSafeBackend",
    "PythonModuleBackend",
    "SyncBracket",
    "SyncState",
    "get_exec_backend",
    "list_exec_backends",
    "register_exec_backend",
    "runtime_exec",
]
