"""G3: obase.telemetry — JSONL 追踪/遥测测试。

覆盖：@traced 同步/异步、ContextVar 共享可变对象铁律（C1，子 Task 不串扰）、
emit 转发、JSONL 落盘/读取、错误记录、注入 emitter。
"""

import asyncio
import json
import pathlib

import pytest

from veya.obase import telemetry


# ── @traced 基础 ──────────────────────────────────────────────────────
def test_traced_sync_records_span():
    trace = telemetry.begin_trace("test-root")

    @telemetry.traced("compute")
    def add(a: int, b: int) -> int:
        return a + b

    with trace:
        assert add(1, 2) == 3

    steps = trace.steps
    assert steps[0]["span"] == "compute" and steps[0]["event"] == "enter"
    assert steps[1]["event"] == "exit"
    assert steps[1]["status"] == "completed"
    assert steps[1]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_traced_async_records_span():
    trace = telemetry.begin_trace("async-root")

    @telemetry.traced("fetch")
    async def fetch(x: int) -> int:
        await asyncio.sleep(0.01)
        return x * 2

    with trace:
        assert await fetch(21) == 42

    events = [s["event"] for s in trace.steps]
    assert events == ["enter", "exit"]


def test_traced_error_records_failed_and_reraises():
    trace = telemetry.begin_trace("err-root")

    @telemetry.traced("boom")
    def boom():
        raise ValueError("bad")

    with trace, pytest.raises(ValueError):
        boom()

    assert trace.steps[-1]["event"] == "error"
    assert trace.steps[-1]["status"] == "failed"
    assert "ValueError" in trace.steps[-1]["error"]


@pytest.mark.asyncio
async def test_traced_cancelled_marks_cancelled():
    trace = telemetry.begin_trace("cancel-root")

    @telemetry.traced("slow")
    async def slow():
        await asyncio.sleep(5)

    with trace, pytest.raises(asyncio.CancelledError):
        task = asyncio.create_task(slow())
        await asyncio.sleep(0.01)
        task.cancel()
        await task

    assert trace.steps[-1]["status"] == "cancelled"


# ── ContextVar 铁律（C1：共享可变对象，子 Task 不串扰） ──────────────
@pytest.mark.asyncio
async def test_contextvar_child_task_accumulates_same_object():
    """子 Task 内 .get() 拿到同一 TraceContext，add_step 累加；.set() 不会污染父。"""
    trace = telemetry.begin_trace("ctx-root")
    telemetry._current.set(trace)
    try:

        async def worker(i: int):
            current = telemetry._current.get()
            assert current is trace  # 同一对象引用
            telemetry.emit({"span": "worker", "event": "step", "i": i})

        await asyncio.gather(*(worker(i) for i in range(5)))

        # 父 context 里 step 已在同一对象上累加
        assert len(trace.steps) == 5
        assert {s["i"] for s in trace.steps} == {0, 1, 2, 3, 4}

        # 子 Task 内 set() 不回传父 context（PEP 567：Task 持独立 context 副本）
        async def bad_set():
            telemetry._current.set(telemetry.begin_trace("shadow"))

        task = asyncio.create_task(bad_set())  # create_task → 独立 context 副本
        await task
        assert telemetry._current.get() is trace
        # 而同一 context 内直接 await 的协程 set() 会传播（同一 Task）
        await bad_set()
        assert telemetry._current.get() is not trace
    finally:
        telemetry._current.set(None)


# ── emit / emitter 注入 ───────────────────────────────────────────────
def test_emit_appends_step_and_forwards_to_emitter():
    trace = telemetry.begin_trace("emit-root")
    forwarded: list[dict] = []
    telemetry.set_emitter(forwarded.append)
    t2 = telemetry._current.set(trace)
    try:
        telemetry.emit({"event": "tool_call", "tool": "write"})
    finally:
        telemetry._current.reset(t2)
        telemetry.set_emitter(None)

    assert len(trace.steps) == 1
    assert trace.steps[0]["tool"] == "write"
    assert forwarded and forwarded[0]["event"] == "tool_call"


def test_emit_without_trace_is_safe():
    telemetry.emit({"event": "orphan"})  # 不 raise


# ── JSONL 汇出 ────────────────────────────────────────────────────────
def test_jsonl_write_and_latest(tmp_path: pathlib.Path):
    trace = telemetry.begin_trace("jsonl-root", meta={"user": "anon-abc"})
    trace.add_step({"event": "start"})
    telemetry.end_trace(trace)

    path = telemetry.jsonl_write(trace, path=tmp_path / "traces.jsonl")
    assert path.exists()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["trace_id"] == trace.trace_id
    assert data["status"] == "completed"
    assert data["meta"]["user"] == "anon-abc"

    # 单源：latest_trace 委托 compat.jsonl_latest
    latest = telemetry.latest_trace(path=path)
    assert latest is not None
    assert latest["trace_id"] == trace.trace_id


def test_jsonl_append_multiple_traces(tmp_path: pathlib.Path):
    path = tmp_path / "multi.jsonl"
    for i in range(3):
        t = telemetry.begin_trace(f"t{i}")
        telemetry.end_trace(t)
        telemetry.jsonl_write(t, path=path)

    latest = telemetry.latest_trace(path=path)
    assert latest["name"] == "t2"


# ── 参数摘要防 PII ────────────────────────────────────────────────────
def test_args_summary_truncates_and_scrubs():
    summary = telemetry._args_summary(
        ("a" * 500,), {"user_id": "secret-123", "big": list(range(100))}
    )
    assert len(summary["arg0"]) <= 81  # 80 + ellipsis
    assert "…" in summary["arg0"]
    assert summary["user_id"] == "secret-123"  # 标量保留（伪名化由调用方负责）
    assert "big" in summary
