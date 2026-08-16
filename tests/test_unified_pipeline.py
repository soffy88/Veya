"""统一流水线总装端到端验证: Phase1→2→3 编排 + evolve_solution 主脑工具 + 注册。"""

from __future__ import annotations

import asyncio

from server.graft_context import GraftContext
from server.reasoning_bank import ReasoningBank
from server.unified_pipeline import (
    evolve_solution_tool,
    register,
    run_pipeline,
)

_STUB = "def add(a, b):\n    raise NotImplementedError\n"
_TEST = (
    "import unittest\n"
    "from solution import add\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
)


def _fake_llm(prompt: str) -> str:
    if "OPERATOR: draft" in prompt:
        return "```python\ndef add(a, b):\n    return a - b\n```"
    if "OPERATOR: debug" in prompt or "OPERATOR: crossover" in prompt:
        return "```python\ndef add(a, b):\n    return a + b\n```"
    if "reusable lesson" in prompt:  # induction
        return (
            '{"situation": "implementing arithmetic helpers", '
            '"pitfall": "sign errors in the first draft", '
            '"fix": "run the unit tests in a sandbox before committing"}'
        )
    return "```python\ndef add(a, b):\n    return a + b\n```"


def test_run_pipeline_composes_all_three_phases(tmp_path):
    bank = ReasoningBank(base_dir=tmp_path)
    graft = GraftContext()
    result = run_pipeline(
        task="Implement add(a, b) in solution",
        workspace_files={"solution.py": _STUB, "test_solution.py": _TEST},
        target_files=["solution.py"],
        llm=_fake_llm,
        bank=bank,
        graft=graft,
        n_branches=2,
        budget=8,
        max_depth=3,
        isolation="none",
    )
    # Phase 2: 演化通关 (solved=测试全绿; 复合分含 diff 惩罚故 <1.0 但应较高)
    assert result.solved and result.best_reward >= 0.75
    assert "a + b" in result.best_files["solution.py"]
    # Phase 1: Graft 上下文块出现在结果里
    assert "Graft dependency map" in result.context_block
    # Phase 3: 归纳出经验并落盘
    assert result.experience is not None
    assert bank.count() == 1
    assert "sandbox" in result.summary().lower()


def test_evolve_solution_tool_reads_and_writes_workspace(tmp_path):
    (tmp_path / "solution.py").write_text(_STUB)
    (tmp_path / "test_solution.py").write_text(_TEST)

    out = asyncio.run(
        evolve_solution_tool(
            task="Implement add(a, b)",
            target_file="solution.py",
            workspace_root=str(tmp_path),
            n_branches=2,
            budget=8,
            _llm=_fake_llm,
            _bank_dir=str(tmp_path / "bank"),
        )
    )
    assert "solved" in out.lower()
    # 最优解已写回目标文件, 测试文件未被动过
    assert "a + b" in (tmp_path / "solution.py").read_text()
    assert (tmp_path / "test_solution.py").read_text() == _TEST


def test_evolve_solution_tool_defaults_to_veya_workspace(tmp_path, monkeypatch):
    """不传 workspace_root 时应落到 VEYA_WORKSPACE (而非进程 cwd) —— 主脑调用的真实路径。"""
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    (tmp_path / "solution.py").write_text(_STUB)
    (tmp_path / "test_solution.py").write_text(_TEST)

    out = asyncio.run(
        evolve_solution_tool(
            task="Implement add(a, b)",
            target_file="solution.py",  # 无 workspace_root
            n_branches=2,
            budget=8,
            _llm=_fake_llm,
            _bank_dir=str(tmp_path / "bank"),
        )
    )
    assert "solved" in out.lower()
    assert "a + b" in (tmp_path / "solution.py").read_text()  # 真的写回了工作区文件


def test_evolve_solution_tool_requires_tests(tmp_path):
    (tmp_path / "solution.py").write_text(_STUB)
    out = asyncio.run(
        evolve_solution_tool(
            task="x",
            target_file="solution.py",
            workspace_root=str(tmp_path),
            _llm=_fake_llm,
        )
    )
    assert "测试" in out  # 无 test_*.py → 拒绝并说明


def test_registered_in_master_registry():
    from server.tool_registry import master_tools

    assert master_tools.has("evolve_solution")
    schema = next(
        s for s in master_tools.get_all_schemas() if s["function"]["name"] == "evolve_solution"
    )
    props = schema["function"]["parameters"]["properties"]
    assert "task" in props and "target_file" in props
    # 测试注入参数 (下划线前缀) 不得泄进 LLM schema
    assert "_llm" not in props and "_bank_dir" not in props


def test_register_is_idempotent():
    from server.tool_registry import master_tools

    register(master_tools)  # 重复注册不应抛错
    assert master_tools.has("evolve_solution")
