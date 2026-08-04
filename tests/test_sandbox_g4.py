"""G4: 沙箱增强测试 — 危险命令前置拦截、参数数组执行、CPU 限制。

覆盖：is_dangerous_command/is_dangerous_argv 单源、execute 拦截、
execute_args 无 shell 注入面、CPU ulimit 前缀、run_script 走参数数组、
tools.py 委托一致（§1.4 守卫测试）。
"""

import pytest

from veya.sandbox import (
    SandboxConfig,
    is_dangerous_argv,
    is_dangerous_command,
)


# ── canonical 危险检测 ────────────────────────────────────────────────
def test_dangerous_command_patterns():
    assert is_dangerous_command("rm -rf /tmp/x")
    assert is_dangerous_command("sudo chmod 777 /etc/passwd")
    assert is_dangerous_command("git reset --hard HEAD")
    assert is_dangerous_command("docker rmi -f ubuntu")
    assert is_dangerous_command("cat x | rm -rf")


def test_safe_commands_allowed():
    assert not is_dangerous_command("echo hello")
    assert not is_dangerous_command("ls -la")
    assert not is_dangerous_command("rm file.txt")  # 单文件 rm 允许
    assert not is_dangerous_command("git status")


def test_dangerous_argv_detection():
    assert is_dangerous_argv(["rm", "-rf", "/tmp"])
    assert is_dangerous_argv(["git", "rebase", "-i", "main"])
    # 保守策略：参数数据中含 rm -rf 也拦截（防绕过，defense-in-depth）
    assert is_dangerous_argv(["echo", "hello world; rm -rf /"])
    # 无害元字符参数放行
    assert not is_dangerous_argv(["echo", "hello; world & friends"])
    assert not is_dangerous_argv(["echo", "$(date)"])


# ── execute 拦截 ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_execute_blocks_dangerous_before_running():
    from veya.sandbox import SafeExecutor

    executor = SafeExecutor(config=SandboxConfig(time_limit=5))
    await executor.start()
    try:
        result = await executor.execute("rm -rf /tmp/nonexistent-veya-test")
        assert result["exit_code"] == -3
        assert "rejected" in result["stderr"].lower()
    finally:
        await executor.stop()


@pytest.mark.asyncio
async def test_execute_allows_when_reject_dangerous_disabled():
    from veya.sandbox import SafeExecutor

    executor = SafeExecutor(
        config=SandboxConfig(time_limit=5, reject_dangerous=False, allow_write=True)
    )
    await executor.start()
    try:
        # rm 单文件（非 -rf）本就不危险；此处验证配置开关不误伤
        result = await executor.execute("echo ok")
        assert result["exit_code"] == 0
    finally:
        await executor.stop()


# ── execute_args 参数数组 ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_execute_args_runs_program():
    from veya.sandbox import SafeExecutor

    executor = SafeExecutor(config=SandboxConfig(time_limit=5))
    await executor.start()
    try:
        result = await executor.execute_args(["echo", "no-shell-injection"])
        assert result["exit_code"] == 0
        assert "no-shell-injection" in result["stdout"]
    finally:
        await executor.stop()


@pytest.mark.asyncio
async def test_execute_args_quoted_shell_metachars_are_literal():
    """参数含 shell 元字符时按字面传递，无注入（G4 核心断言）。"""
    from veya.sandbox import SafeExecutor

    executor = SafeExecutor(config=SandboxConfig(time_limit=5))
    await executor.start()
    try:
        result = await executor.execute_args(["echo", "safe; touch /tmp/pwned-3o && echo pwned"])
        assert result["exit_code"] == 0
        assert result["stdout"] == "safe; touch /tmp/pwned-3o && echo pwned"
        assert not result["stdout"].startswith("safe\n")  # 未执行分号后的命令
    finally:
        await executor.stop()


@pytest.mark.asyncio
async def test_execute_args_blocks_dangerous():
    from veya.sandbox import SafeExecutor

    executor = SafeExecutor(config=SandboxConfig(time_limit=5))
    await executor.start()
    try:
        result = await executor.execute_args(["rm", "-rf", "/tmp"])
        assert result["exit_code"] == -3
    finally:
        await executor.stop()


@pytest.mark.asyncio
async def test_execute_args_empty_argv():
    from veya.sandbox import SafeExecutor

    executor = SafeExecutor(config=SandboxConfig(time_limit=5))
    await executor.start()
    try:
        result = await executor.execute_args([])
        assert result["exit_code"] == -2
    finally:
        await executor.stop()


# ── CPU 限制 ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_cpu_limit_prefix_applied_to_child():
    from veya.sandbox import SafeExecutor

    executor = SafeExecutor(config=SandboxConfig(time_limit=10, cpu_limit=5))
    await executor.start()
    try:
        # ulimit -t 在子进程可读（千兆级单位: 秒）
        result = await executor.execute("ulimit -t")
        assert result["exit_code"] == 0
        assert result["stdout"].strip() == "5"
    finally:
        await executor.stop()


@pytest.mark.asyncio
async def test_memory_and_cpu_prefixes_combined():
    from veya.sandbox import SafeExecutor

    executor = SafeExecutor(
        config=SandboxConfig(time_limit=10, cpu_limit=3, memory_limit=64 * 1024 * 1024)
    )
    await executor.start()
    try:
        result = await executor.execute("ulimit -v; ulimit -t")
        assert result["exit_code"] == 0
        mem_kb, cpu_sec = result["stdout"].split()
        assert int(mem_kb) == 65536  # 64 MiB
        assert int(cpu_sec) == 3
    finally:
        await executor.stop()


# ── run_script 走参数数组 ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_run_script_uses_args_execution():
    from veya.sandbox import SafeExecutor

    executor = SafeExecutor(config=SandboxConfig(time_limit=5))
    await executor.start()
    try:
        result = await executor.run_script("import sys\nprint('script-ok', sys.version_info[0])")
        assert result["exit_code"] == 0
        assert result["stdout"].startswith("script-ok 3")
    finally:
        await executor.stop()


# ── 单源守卫：tools.py 委托与 sandbox canonical 一致（§1.4） ─────────
def test_tools_delegates_to_canonical_danger_check():
    from veya.tools import TerminalTool

    tool = TerminalTool()
    # 同一输入，两条路径行为一致（§1.4 守卫：改一份漏另一份的防漂移断言）
    assert tool._is_safe_command("rm -rf /") is False
    assert is_dangerous_command("rm -rf /") is True
    assert tool._is_safe_command("ls") is True
    assert is_dangerous_command("ls") is False
    assert tool._is_safe_command("git reset --hard HEAD") is False
    assert is_dangerous_command("git reset --hard HEAD") is True
