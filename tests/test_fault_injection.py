"""docs/VEYA_10_OF_10_PLAN.md §28「增加 fault-injection suite」。

真实故障注入用例——不是给已有测试改名凑数：故意让工具执行超时/抛异常/查无
此工具，验证 `MasterToolRegistry.execute()`（全仓库唯一的工具执行收口点，见
`docs/dev/rfc-10-observability-scoping.md` §1）在这三种故障下都收敛成
`ToolExecutionError`（不是裸异常/不是挂起），且 `veya.obase.telemetry` 埋点
记录了正确的故障状态。另外覆盖 `retry_with_backoff`（`veya/obase/compat.py`）
耗尽重试后正确重新抛出最后一次异常，而不是吞掉或死循环。
"""

from __future__ import annotations

import asyncio

import pytest

from server.tool_registry import MasterToolRegistry, ToolExecutionError
from veya.obase import telemetry


def _add_tool(reg: MasterToolRegistry, name: str, func, *, timeout_s: float | None = None) -> None:
    reg.register(
        name,
        f"fault-injection fixture: {name}",
        {"type": "object", "properties": {}},
        func,
        timeout_s=timeout_s,
    )


class _SpanCapture:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, event: dict) -> None:
        self.events.append(event)


@pytest.fixture
def span_capture():
    cap = _SpanCapture()
    token = telemetry.set_emitter(cap)
    try:
        yield cap
    finally:
        telemetry._emitter_ctx.reset(token)


@pytest.mark.asyncio
async def test_tool_timeout_surfaces_as_tool_execution_error(span_capture):
    reg = MasterToolRegistry()

    async def _hangs(**kwargs):
        await asyncio.sleep(10)
        return "never"

    _add_tool(reg, "hangs", _hangs, timeout_s=0.05)

    with pytest.raises(ToolExecutionError, match="timed out"):
        await reg.execute("hangs", {})

    error_spans = [e for e in span_capture.events if e.get("event") == "error"]
    assert error_spans, "超时应该产生一条 error span"
    assert error_spans[-1]["status"] == "timeout"
    assert error_spans[-1]["tool"] == "hangs"


@pytest.mark.asyncio
async def test_tool_unexpected_exception_wrapped_not_leaked(span_capture):
    reg = MasterToolRegistry()

    async def _boom(**kwargs):
        raise ValueError("simulated internal failure")

    _add_tool(reg, "boom", _boom)

    with pytest.raises(ToolExecutionError, match="ValueError") as exc_info:
        await reg.execute("boom", {})
    # 原始异常类型信息应该保留在包装后的消息里，不能被吞成无信息量的通用错误。
    assert "simulated internal failure" in str(exc_info.value)
    # 原始异常通过 __cause__ 保留，方便上层需要时做更细的分类。
    assert isinstance(exc_info.value.__cause__, ValueError)

    error_spans = [e for e in span_capture.events if e.get("event") == "error"]
    assert error_spans[-1]["status"] == "failed"
    assert "ValueError" in error_spans[-1]["error"]


@pytest.mark.asyncio
async def test_tool_not_found_does_not_crash_caller():
    reg = MasterToolRegistry()
    with pytest.raises(ToolExecutionError, match="not found"):
        await reg.execute("does_not_exist", {})


@pytest.mark.asyncio
async def test_tool_sync_exception_also_wrapped(span_capture):
    """物理函数是同步的（不是 async def）也要经过同一条故障处理路径。"""
    reg = MasterToolRegistry()

    def _sync_boom(**kwargs):
        raise RuntimeError("sync failure")

    _add_tool(reg, "sync_boom", _sync_boom)

    with pytest.raises(ToolExecutionError, match="sync failure"):
        await reg.execute("sync_boom", {})


@pytest.mark.asyncio
async def test_retry_with_backoff_exhausts_and_reraises_last_exception():
    from veya.obase.compat import retry_with_backoff

    attempts = 0

    async def _always_fails():
        nonlocal attempts
        attempts += 1
        raise ConnectionError(f"attempt {attempts}")

    with pytest.raises(ConnectionError, match="attempt 3"):
        await retry_with_backoff(_always_fails, max_attempts=3, base_delay=0.001)
    assert attempts == 3


@pytest.mark.asyncio
async def test_retry_with_backoff_recovers_after_transient_failures():
    from veya.obase.compat import retry_with_backoff

    attempts = 0

    async def _fails_twice_then_succeeds():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("transient")
        return "ok"

    result = await retry_with_backoff(_fails_twice_then_succeeds, max_attempts=5, base_delay=0.001)
    assert result == "ok"
    assert attempts == 3


@pytest.mark.asyncio
async def test_retry_with_backoff_does_not_retry_non_retryable_exceptions():
    from veya.obase.compat import retry_with_backoff

    attempts = 0

    async def _raises_value_error():
        nonlocal attempts
        attempts += 1
        raise ValueError("not retryable by default")

    with pytest.raises(ValueError):
        await retry_with_backoff(_raises_value_error, max_attempts=3, base_delay=0.001)
    # 默认 retryable=(ConnectionError,)，ValueError 不该被重试。
    assert attempts == 1
