"""goal_run verify — 机械核实测试数字通过率优先于 LLM 主观判定 (autoresearch 内化)。"""

from __future__ import annotations

import pytest

from server.goal_run.models import TaskNode, TaskStatus
from server.goal_run.verify import verify_task


def _task(acceptance: list[str]) -> TaskNode:
    return TaskNode(
        id="t1",
        title="run tests",
        instruction="run the test suite",
        acceptance=acceptance,
        depends_on=[],
        assignee="hicode",
        status=TaskStatus.running,
    )


@pytest.mark.asyncio
async def test_mechanical_pass_skips_llm(tmp_path, monkeypatch):
    async def _boom(*_a, **_k):
        raise AssertionError("LLM 判定不应被调用: 数字信号已机械核实")

    monkeypatch.setattr("server.goal_run.verify._llm_check", _boom)

    task = _task(["所有单元测试通过"])
    result = await verify_task(task, "5 passed, 0 failed in 0.42s", str(tmp_path))
    assert result.passed is True
    assert "5 passed" in result.summary


@pytest.mark.asyncio
async def test_mechanical_fail_skips_llm(tmp_path, monkeypatch):
    async def _boom(*_a, **_k):
        raise AssertionError("LLM 判定不应被调用: 数字信号已机械核实")

    monkeypatch.setattr("server.goal_run.verify._llm_check", _boom)

    task = _task(["all tests pass"])
    result = await verify_task(task, "3 passed, 2 failed in 0.10s", str(tmp_path))
    assert result.passed is False
    assert "2 failed" in result.summary


@pytest.mark.asyncio
async def test_no_numeric_signal_falls_back_to_llm(tmp_path, monkeypatch):
    async def _fake_llm(*_a, **_k):
        return True, "LLM 判定通过"

    monkeypatch.setattr("server.goal_run.verify._llm_check", _fake_llm)

    task = _task(["所有单元测试通过"])
    result = await verify_task(
        task, "tests finished without a parseable summary line", str(tmp_path)
    )
    assert result.passed is True
    assert result.summary == "LLM 判定通过"


@pytest.mark.asyncio
async def test_non_test_acceptance_falls_back_to_llm(tmp_path, monkeypatch):
    async def _fake_llm(*_a, **_k):
        return True, "LLM 判定通过"

    monkeypatch.setattr("server.goal_run.verify._llm_check", _fake_llm)

    task = _task(["文档已更新"])
    result = await verify_task(task, "5 passed, 0 failed", str(tmp_path))
    assert result.passed is True
    assert result.summary == "LLM 判定通过"
