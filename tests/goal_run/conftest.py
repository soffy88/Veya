"""tests/goal_run 目录级 fixture。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_performance_store(tmp_path, monkeypatch):
    """runner.py::_process_one_task 旁路喂 VAOM PerformanceStore(见
    server/capability_model.py)，默认单例落盘 ~/.veya/vaom_performance.jsonl。
    goal_run 测试大量直接调 _process_one_task/project_run_goal，不隔离会真的
    往用户生产文件里写测试数据(踩过一次坑，见 memory project_veya_pi_gap_audit
    里 test_retry_branch.py 同类教训)。autouse 保证这个目录下所有测试都不例外。
    """
    from server.capability_model import PerformanceStore

    monkeypatch.setattr(
        "server.goal_run.runner.performance_store",
        PerformanceStore(storage_path=tmp_path / "perf_store_isolated.jsonl"),
    )


@pytest.fixture(autouse=True)
def _isolate_memory_controller(tmp_path, monkeypatch):
    """runner.py::_finalize_episode 旁路喂 VAOM MemoryController(见
    server/memory_controller.py)，默认单例落盘 ~/.veya/vaom_memory_records.json。
    同 _isolate_performance_store 的理由——这次是提前隔离，不是踩了坑才补。
    """
    from server.memory_controller import MemoryController, _MemoryStore

    monkeypatch.setattr(
        "server.goal_run.runner.memory_controller",
        MemoryController(_MemoryStore(storage_path=tmp_path / "memory_isolated.json")),
    )


@pytest.fixture(autouse=True)
def _isolate_durable_runtime(tmp_path, monkeypatch):
    """Never let the goal-run suite write to the deployment PostgreSQL.

    Production enables the durable runtime through the container environment;
    tests that exercise durable GoalRun integration opt into their own
    function-scoped SQLite runtime explicitly.
    """
    from runtime.execution.runtime import DurableExecutionRuntime, DurableRuntimeConfig

    runtime = DurableExecutionRuntime(
        DurableRuntimeConfig(enabled=False, sqlite_path=str(tmp_path / "durable.sqlite3"))
    )
    monkeypatch.setattr("runtime.execution.runtime._default_runtime", runtime)
