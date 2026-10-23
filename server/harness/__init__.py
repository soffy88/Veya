"""Veya × harness2 Phase 0 integration (流程验证).

本包是 harness2 的 *外部 executor adapter*（集成文档方案 A）：把 Veya 包装成
harness2 的 `BenchmarkAdapter`，并提供一个只读导出器，把 Veya 当前的 prompt /
技能 / 工具说明导出为 harness2 的 c1..c8 组件树。

Phase 0 不改动冻结主链路（master_agent / tool_registry 行为不变），只用 Veya
已有的隔离执行原语 `server.agent_loop_bridge.run_strict_chat` 作为确定性后端，
配合 harness2 的 `DemoProposer`（无模型调用）跑通 k=1 全循环。
"""

__all__ = [
    "VeyaBenchmarkAdapter",
    "VeyaDemoProposer",
    "VeyaProposer",
    "build_harness_tree",
    "export_base_harness",
    "import_harness2_core",
    "proposer_enabled",
    "read_veya_sources",
    "veya_substrate",
]
