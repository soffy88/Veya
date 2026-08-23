"""server.vaom_query_tools 测试(MasterAgent P5 只读查询工具, 见
docs/dev/rfc-01-vaom.md, docs/VEYA_3.0_GAP_AUDIT.md)。

用 monkeypatch 换掉 server.capability_model / server.memory_controller 的模块级
单例, 不碰真实 ~/.veya 文件——这两个函数内部是局部 import(`from
server.capability_model import harness_registry, performance_store`), 所以隔离
点是 capability_model.py/memory_controller.py 自己的模块属性, 不是
vaom_query_tools.py 的(那里没有同名模块级绑定)。
"""

from __future__ import annotations

from server.capability_model import (
    HarnessRegistry,
    HarnessSpec,
    PerformanceStore,
    _JsonRegistryStore,
)
from server.memory_controller import MemoryController, _MemoryStore
from server.vaom_query_tools import harness_performance_query, memory_recall_project_lessons


def _isolated_registry_and_store(tmp_path):
    registry = HarnessRegistry(_JsonRegistryStore(storage_path=tmp_path / "registry.json"))
    store = PerformanceStore(storage_path=tmp_path / "perf.jsonl")
    return registry, store


# ── harness_performance_query ───────────────────────────────────────────


def test_harness_query_no_data_for_known_harness(tmp_path, monkeypatch):
    registry, store = _isolated_registry_and_store(tmp_path)
    registry.register(HarnessSpec(harness_id="hicode", version="1"))
    monkeypatch.setattr("server.capability_model.harness_registry", registry)
    monkeypatch.setattr("server.capability_model.performance_store", store)

    result = harness_performance_query(harness_id="hicode")

    assert result["status"] == "no_data"
    assert result["harness_id"] == "hicode"


def test_harness_query_returns_real_profile(tmp_path, monkeypatch):
    registry, store = _isolated_registry_and_store(tmp_path)
    registry.register(HarnessSpec(harness_id="hicode", version="1"))
    store.record_outcome(harness_id="hicode", task_archetype="goal_run_task", success=True)
    store.record_outcome(harness_id="hicode", task_archetype="goal_run_task", success=False)
    monkeypatch.setattr("server.capability_model.harness_registry", registry)
    monkeypatch.setattr("server.capability_model.performance_store", store)

    result = harness_performance_query(harness_id="hicode", task_archetype="goal_run_task")

    assert result["status"] == "ok"
    assert result["sample_size"] == 2
    assert result["success_rate"] == 0.5


def test_harness_query_without_id_compares_all_known(tmp_path, monkeypatch):
    registry, store = _isolated_registry_and_store(tmp_path)
    registry.register(HarnessSpec(harness_id="hicode", version="1"))
    registry.register(HarnessSpec(harness_id="dsh", version="1"))
    store.record_outcome(harness_id="hicode", task_archetype="t", success=True)
    monkeypatch.setattr("server.capability_model.harness_registry", registry)
    monkeypatch.setattr("server.capability_model.performance_store", store)

    result = harness_performance_query(task_archetype="t")

    assert result["status"] == "ok"
    assert set(result["profiles"].keys()) == {"hicode"}  # dsh 没数据, compare 只返回有数据的


def test_harness_query_no_data_at_all(tmp_path, monkeypatch):
    registry, store = _isolated_registry_and_store(tmp_path)
    registry.register(HarnessSpec(harness_id="hicode", version="1"))
    monkeypatch.setattr("server.capability_model.harness_registry", registry)
    monkeypatch.setattr("server.capability_model.performance_store", store)

    result = harness_performance_query()

    assert result["status"] == "no_data"
    assert result["known_harnesses"] == ["hicode"]


# ── memory_recall_project_lessons ───────────────────────────────────────


def test_memory_recall_no_match(tmp_path, monkeypatch):
    memory = MemoryController(_MemoryStore(storage_path=tmp_path / "memory.json"))
    monkeypatch.setattr("server.memory_controller.memory_controller", memory)

    result = memory_recall_project_lessons(query="nonexistent")

    assert result["status"] == "no_data"


def test_memory_recall_returns_matching_lessons(tmp_path, monkeypatch):
    memory = MemoryController(_MemoryStore(storage_path=tmp_path / "memory.json"))
    memory.observe("先跑迁移脚本再动代码", scope="project", provenance="goal_run:g1")
    monkeypatch.setattr("server.memory_controller.memory_controller", memory)

    result = memory_recall_project_lessons(query="迁移脚本")

    assert result["status"] == "ok"
    assert len(result["lessons"]) == 1
    assert result["lessons"][0]["content"] == "先跑迁移脚本再动代码"
    assert result["lessons"][0]["provenance"] == "goal_run:g1"


def test_memory_recall_filters_by_scope(tmp_path, monkeypatch):
    memory = MemoryController(_MemoryStore(storage_path=tmp_path / "memory.json"))
    memory.observe("project lesson", scope="project")
    memory.observe("global lesson", scope="global")
    monkeypatch.setattr("server.memory_controller.memory_controller", memory)

    result = memory_recall_project_lessons(scope="global")

    assert result["status"] == "ok"
    assert len(result["lessons"]) == 1
    assert result["lessons"][0]["scope"] == "global"


# ── 确认真的挂进了 MasterAgent 工具面 ─────────────────────────────────────


def test_tools_registered_in_master_tools():
    from server.tool_registry import master_tools

    assert master_tools.has("harness_performance_query")
    assert master_tools.has("memory_recall_project_lessons")
