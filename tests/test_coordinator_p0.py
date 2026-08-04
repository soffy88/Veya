from unittest.mock import AsyncMock, patch

import pytest

from hicode.context import SmartContextManager
from hicode.streaming import StreamEventType, StreamingManager
from server.coordinator import Coordinator


@pytest.mark.asyncio
async def test_context_manager():
    """测试上下文管理器"""
    ctx = SmartContextManager(max_tokens=12, keep_recent=2)

    # 添加消息
    ctx.add_message("user", "Hello")
    ctx.add_message("assistant", "Hi there!")
    ctx.add_message("user", "How are you?")
    ctx.add_message("assistant", "I am good!")

    # 检查统计
    stats = ctx.get_stats()
    assert stats["total_messages"] == 4
    assert stats["total_summaries"] == 0

    # 添加更多消息触发压缩
    for i in range(5):
        ctx.add_message("user", f"Test message {i}")
        ctx.add_message("assistant", f"Response {i}")

    stats = ctx.get_stats()
    assert stats["total_summaries"] > 0
    assert stats["total_messages"] <= 2  # 只保留最近2条


@pytest.mark.asyncio
async def test_streaming_manager():
    """测试流式管理器"""
    manager = StreamingManager(stream_id="test_stream")
    events = []

    def callback(event):
        events.append(event)

    manager.subscribe(callback)

    # 测试事件发射
    await manager.emit(StreamEventType.START, {"test": "start"})
    await manager.emit(StreamEventType.TOKEN, {"text": "Hello"})

    # 检查前两个事件
    assert len(events) == 2
    assert events[0].type == StreamEventType.START
    assert events[1].type == StreamEventType.TOKEN

    # 测试中断 (interrupt before COMPLETE)
    await manager.interrupt()
    assert manager.status == "interrupted"
    assert len(events) == 3
    assert events[2].type == StreamEventType.INTERRUPTED

    # 完成事件在中断后不再有效
    await manager.emit(StreamEventType.COMPLETE, {"final": "done"})
    assert len(events) == 3  # COMPLETE was rejected because status is INTERRUPTED


@pytest.mark.asyncio
async def test_coordinator_streaming():
    """测试协调器流式输出"""
    coordinator = Coordinator(enable_streaming=True)
    session_id = "test_session"

    # 模拟执行
    command = {"text": "Test command", "model": "test-model"}

    # 重写 squad 执行以避免实际 LLM 调用
    async def mock_run_squads(orchestrator, plan, *, session_id=None):
        return [
            SquadResult(
                squad_id="test",
                role="researcher",
                status="success",
                output="Test response",
                cost_usd=0.0,
            )
        ]

    from server.coordinator import SquadResult

    coordinator._run_squads = mock_run_squads

    # 执行命令
    await coordinator.handle(command, session_id=session_id)

    # 检查流式管理器
    assert session_id in coordinator.streaming_managers
    streaming_manager = coordinator.streaming_managers[session_id]

    # 检查事件历史
    history = streaming_manager.get_history()
    assert any(e["type"] == "start" for e in history)
    assert any(e["type"] == "progress" for e in history)
    assert any(e["type"] == "complete" for e in history)


@pytest.mark.asyncio
async def test_coordinator_parallel_execution():
    """测试协调器并行执行"""
    coordinator = Coordinator()

    # 创建测试任务
    squads = [
        {"squad_id": "1", "role": "test", "command": {"text": "Task 1"}},
        {"squad_id": "2", "role": "test", "command": {"text": "Task 2"}},
        {"squad_id": "3", "role": "test", "command": {"text": "Task 3"}},
    ]

    # 模拟执行
    with patch.object(coordinator, "_execute_squad", new=AsyncMock()) as mock_execute:
        mock_execute.return_value = {"status": "success", "output": "Result", "cost_usd": 0.0}

        # 使用并行执行器
        tasks = [(coordinator._execute_squad, (s, "test_session")) for s in squads]
        results = await coordinator.parallel_executor.execute_all(tasks)

        # 检查调用
        assert mock_execute.call_count == 3
        assert all(isinstance(r, dict) for r in results)


@pytest.mark.asyncio
async def test_coordinator_interrupt():
    """测试中断功能"""
    coordinator = Coordinator(enable_streaming=True)
    session_id = "test_session"

    # 创建流式会话
    await coordinator.handle({"text": "Test"}, session_id=session_id)

    # 测试中断
    result = await coordinator.handle_interrupt(session_id)
    assert result["status"] == "interrupted"
    assert session_id not in coordinator.streaming_managers
    assert session_id not in coordinator.context_managers
