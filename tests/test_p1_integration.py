"""
P1 能力集成测试
"""

import asyncio

import pytest

from server.coordinator import Coordinator


@pytest.mark.asyncio
async def test_ast_analysis_integration():
    """测试 AST 分析集成"""
    coordinator = Coordinator()

    # 测试项目分析
    result = await coordinator.analyze_project(".")
    assert result["status"] == "success"
    assert "stats" in result
    assert "top_functions" in result
    assert "dependency_graph" in result

    print(f"AST Stats: {result['stats']}")
    print(f"Top Functions: {result['top_functions'][:3]}")


@pytest.mark.asyncio
async def test_tool_execution_integration():
    """测试工具执行集成"""
    coordinator = Coordinator()

    # 测试安全执行 Git 命令
    result = await coordinator.execute_tool(
        "git", {"command": "status", "path": "."}, use_sandbox=False
    )

    assert "status" in result
    assert "output" in result
    print(f"Tool Status: {result['status']}")
    print(f"Tool Output: {result['output'][:200]}")


@pytest.mark.asyncio
async def test_sandbox_execution_integration():
    """测试沙箱执行集成"""
    from hicode.sandbox import SafeExecutor

    executor = SafeExecutor()
    await executor.start()

    try:
        # 测试命令执行
        result = await executor.execute("echo 'Hello from sandbox'")
        assert result["exit_code"] == 0
        assert "Hello from sandbox" in result["stdout"]

        # 测试脚本执行
        script = """
import time
print("Script started")
time.sleep(0.5)
print("Script completed")
"""
        result = await executor.run_script(script)
        assert result["exit_code"] == 0
        assert "Script completed" in result["stdout"]
    finally:
        await executor.stop()


@pytest.mark.asyncio
async def test_sandbox_rlimit_restored_after_capped_execution():
    """Regression: RLIMIT_AS must be restored after a memory-capped sandbox run.

    Previously setup_environment() set both soft+hard RLIMIT_AS to memory_limit,
    permanently capping the host process address space (the hard limit can never
    be raised again). That poisoned every later heavy import / coverage
    measurement in the same process (MemoryError, 0% coverage).
    """
    import resource

    from hicode.sandbox import SafeExecutor, SandboxConfig

    before = resource.getrlimit(resource.RLIMIT_AS)
    executor = SafeExecutor(config=SandboxConfig(memory_limit=100 * 1024 * 1024))
    await executor.start()
    try:
        result = await executor.execute("echo capped")
        assert result["exit_code"] == 0
    finally:
        await executor.stop()

    after = resource.getrlimit(resource.RLIMIT_AS)
    assert after == before, f"RLIMIT_AS not restored: before={before}, after={after}"

    # Heavy module imports must still succeed after the capped run.
    import plotly  # noqa: F401

    assert True


@pytest.mark.asyncio
async def test_sandbox_host_rlimit_never_lowered():
    """Host RLIMIT_AS must stay untouched during a memory-capped sandbox run.

    Limits are applied to the child process only (POSIX ``ulimit -v`` prefix), so
    the host never risks starvation (imports, coverage, pytest) while a sandboxed
    command is running.
    """
    import resource
    import time

    from hicode.sandbox import SafeExecutor, SandboxConfig

    before = resource.getrlimit(resource.RLIMIT_AS)
    executor = SafeExecutor(config=SandboxConfig(memory_limit=64 * 1024 * 1024))
    await executor.start()
    try:
        probe = asyncio.create_task(
            asyncio.to_thread(lambda: (time.sleep(0.15), resource.getrlimit(resource.RLIMIT_AS)))
        )
        result = await executor.execute("sleep 0.6 && echo during")
        _, during = await probe
        assert result["exit_code"] == 0
        assert during == before, f"host RLIMIT_AS changed during run: {during}"
    finally:
        await executor.stop()


@pytest.mark.asyncio
async def test_sandbox_child_memory_limit_enforced():
    """The child process must observe the configured memory limit."""
    from hicode.sandbox import SafeExecutor, SandboxConfig

    executor = SafeExecutor(config=SandboxConfig(memory_limit=100 * 1024 * 1024))
    await executor.start()
    try:
        result = await executor.execute("ulimit -v")
        assert result["exit_code"] == 0
        # ulimit -v reports KiB; 100 MiB == 102400 KiB
        assert result["stdout"].strip() == "102400"
    finally:
        await executor.stop()


@pytest.mark.asyncio
async def test_ast_analyzer():
    """测试 AST 分析器"""
    from hicode.ast import create_ast_analyzer

    analyzer = create_ast_analyzer()
    stats = analyzer.analyze_project(".")

    assert "symbol_count" in stats
    assert stats["symbol_count"] >= 0

    # 测试符号搜索
    functions = [s for s in analyzer.symbols.values() if s.type == "function"]
    if functions:
        symbol = functions[0]
        results = analyzer.search_by_signature(symbol.name)
        assert len(results) > 0

        # 测试依赖图
        graph = analyzer.get_call_graph()
        assert isinstance(graph, dict)

        # 测试引用查找
        refs = analyzer.find_references(symbol.name)
        assert isinstance(refs, list)


@pytest.mark.asyncio
async def test_smart_tools():
    """测试智能工具"""
    from hicode.tools import create_tool_executor

    executor = create_tool_executor()

    # 测试 Git 工具
    git_result = await executor.execute_tool("git", command="status", path=".")
    assert git_result.status.value in ["success", "failed"]

    # 测试终端工具
    terminal_result = await executor.execute_tool("terminal", command='echo "test"', path=".")
    assert terminal_result.status.value in ["success", "failed"]

    # 测试并行执行
    results = await executor.execute_all(
        [
            ("git", {"command": "status", "path": "."}),
            ("terminal", {"command": 'echo "parallel"', "path": "."}),
        ]
    )

    assert len(results) == 2

    # 测试工具建议
    suggestions = executor.get_tool_suggestions("git status")
    assert isinstance(suggestions, list)


@pytest.mark.asyncio
async def test_sandbox_resource_limits():
    """测试沙箱资源限制"""
    from hicode.sandbox import SandboxConfig, create_safe_executor

    config = SandboxConfig(
        memory_limit=50 * 1024 * 1024,  # 50MB
        time_limit=2.0,  # 2 seconds
        audit_enabled=True,
    )

    executor = create_safe_executor(config)
    await executor.start()

    try:
        # 测试超时
        result = await executor.execute("sleep 10")
        # 由于超时，应该会失败或返回错误
        assert result["exit_code"] != 0 or "timeout" in result["stderr"].lower()
    finally:
        await executor.stop()


@pytest.mark.asyncio
async def test_context_and_streaming_with_p1():
    """测试 P0 + P1 集成"""
    from hicode.context import SmartContextManager
    from hicode.streaming import StreamEventType, create_stream_manager

    # 创建上下文和流
    ctx = SmartContextManager(max_tokens=10000)
    stream = create_stream_manager()

    # 添加消息
    ctx.add_message("user", "创建一个 Flask API")
    ctx.add_message("assistant", "好的，我将为您创建 Flask API。")

    # 流式发送事件
    await stream.emit(StreamEventType.TOKEN, {"text": "Hello"})

    # 验证
    assert len(stream.get_history()) == 1
    assert ctx.estimate_total_tokens() > 0

    # 测试中断 (must interrupt before COMPLETE)
    await stream.interrupt()
    assert stream.status.value == "interrupted"

    # COMPLETE after interrupt is rejected
    await stream.emit(StreamEventType.COMPLETE, {"final": "done"})
    assert len(stream.get_history()) == 2  # TOKEN + INTERRUPTED


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
