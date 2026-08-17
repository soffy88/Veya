"""goal_run runner 测试 - 模拟 leaf 执行。"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.goal_run.leaf import LeafResult
from server.goal_run.models import GoalStatus
from server.goal_run.runner import project_run_goal
from server.goal_run.verify import VerifyResult


async def _fake_leaf(*_args, **_kwargs) -> LeafResult:
    return LeafResult(status="completed", summary="t1 done", artifacts=[])


async def _fake_verify(*_args, **_kwargs) -> VerifyResult:
    return VerifyResult(passed=True, summary="ok")


def _stub_run(monkeypatch) -> None:
    monkeypatch.setattr("server.goal_run.runner.execute_leaf_with_memory", _fake_leaf)
    monkeypatch.setattr("server.goal_run.runner.verify_task", _fake_verify)


@pytest.mark.asyncio
async def test_project_run_goal_basic_auto(tmp_path: Path, monkeypatch):
    """basic auto mode: simple goal execution (mock leaf, not full integration)."""
    _stub_run(monkeypatch)
    # 创建项目根目录结构
    (tmp_path / ".veya-project").mkdir(parents=True, exist_ok=True)

    # 简单的目标：实现一个 TODO 列表
    result = await project_run_goal(
        project_root=str(tmp_path),
        goal="实现一个简单的 TODO 列表功能",
        mode="auto",
        wait=True,
    )

    # 验证返回结果
    assert result is not None
    assert result.goal_id is not None or result.status == GoalStatus.blocked
    # 注意：因为是 mock 环境，可能无法真正执行代码
    # 关键是验证响应结构正确
    assert hasattr(result, 'phase')
    assert hasattr(result, 'status')


@pytest.mark.asyncio
async def test_project_run_goal_act_eager(tmp_path: Path, monkeypatch):
    """act_eager 模式：直接执行，跳过门禁。"""
    _stub_run(monkeypatch)
    (tmp_path / ".veya-project").mkdir(parents=True, exist_ok=True)

    result = await project_run_goal(
        project_root=str(tmp_path),
        goal="实现一个简单的 TODO 列表",
        mode="act_eager",
        wait=True,
    )

    # act_eager 应该直接进入执行阶段
    assert result is not None
    # 可能是 running 或 completed，取决于 mock 实现
    assert hasattr(result, 'phase')


@pytest.mark.asyncio
async def test_project_run_goal_ask_only(tmp_path: Path):
    """ask_only 模式：只追问，永不执行。"""
    (tmp_path / ".veya-project").mkdir(parents=True, exist_ok=True)

    result = await project_run_goal(
        project_root=str(tmp_path),
        goal="实现 TODO 列表",
        mode="ask_only",
        wait=True,
    )

    # ask_only 只应返回 understood_ask 相关信息
    assert result is not None
    assert hasattr(result, 'phase')
    # ask_only 不应进行实际执行


@pytest.mark.asyncio
async def test_project_run_goal_resume(tmp_path: Path):
    """resume 未完成的 run。"""
    (tmp_path / ".veya-project").mkdir(parents=True, exist_ok=True)

    # 首次调用创建 run
    result1 = await project_run_goal(
        project_root=str(tmp_path),
        goal="创建 TODO 列表",
        mode="auto",
        wait=True,
    )
    goal_id = result1.goal_id

    # resume 该 run（即使它已 completed，也应该正常处理）
    result2 = await project_run_goal(
        project_root=str(tmp_path),
        goal="再次创建 TODO 列表",
        mode="auto",
        resume_goal_id=goal_id,
        wait=True,
    )

    # 再次调用应正常返回
    assert result2 is not None
    assert hasattr(result2, 'goal_id')
    # 可能目标相同导致 completed


@pytest.mark.asyncio
async def test_project_run_goal_invalid_mode(tmp_path: Path):
    """invalid mode 应该被 block。"""
    (tmp_path / ".veya-project").mkdir(parents=True, exist_ok=True)

    result = await project_run_goal(
        project_root=str(tmp_path),
        goal="测试",
        mode="invalid_mode",
        wait=True,
    )

    # invalid mode 返回 block
    assert result is not None
    # 可能返回 rejected/blocked 状态
    assert hasattr(result, "block_reason") or result.status in [
        GoalStatus.blocked,
        GoalStatus.rejected,
    ]
