"""veya/oprim/shell — 命令执行原子操作（物理触手，捕获 stdout/stderr）。

阶段 3 原子元素：oprim_shell_exec / oprim_shell_exec_args / oprim_shell_run_script。

规则：
- 所有执行经注入的 VfsSandbox 句柄（默认 container 全局句柄）——沙箱内
  执行、危险命令拦截、资源限制/审计由沙盒负责；
- 本层只透传命令与统一结果结构（SandboxResult），不含任何业务判断；
- 不可信输入必须走 ``shell_exec_args``（argv 数组，无 shell 注入面）。
"""

from __future__ import annotations

from typing import Any

from veya.obase.interfaces import SandboxResult


def _sandbox_of(sandbox: Any) -> Any:
    if sandbox is not None:
        return sandbox
    from veya.obase.container import get_sandbox

    return get_sandbox()


async def shell_exec(command: str, sandbox: Any = None, *, timeout: float | None = None) -> SandboxResult:
    """Shell 语义执行（仅限可信内部调用；危险命令由沙盒拦截）。"""
    return await _sandbox_of(sandbox).execute(command, timeout=timeout)  # type: ignore[attr-defined]


async def shell_exec_args(argv: list[str], sandbox: Any = None, *, timeout: float | None = None) -> SandboxResult:
    """argv 数组执行：无 shell 注入面，不可信输入的默认入口。"""
    return await _sandbox_of(sandbox).execute_args(argv, timeout=timeout)  # type: ignore[attr-defined]


async def shell_run_script(script: str, sandbox: Any = None, *, timeout: float | None = None) -> SandboxResult:
    """沙盒内运行脚本。"""
    return await _sandbox_of(sandbox).run_script(script, timeout=timeout)  # type: ignore[attr-defined]


__all__ = ["shell_exec", "shell_exec_args", "shell_run_script"]
