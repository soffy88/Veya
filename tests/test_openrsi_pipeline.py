"""OpenRSI 演化核心端到端验证 (hermetic, 用确定性假 LLM 驱动真实 oprim 沙盒/MCTS)。

场景: 让演化引擎实现 add(a, b)=a+b。假 LLM 的 draft 故意写错 (a-b) → 沙盒测试挂 →
MCTS 触发 debug 算子 → 写对 → 通过率 1.0 → 终止。验证 draft→沙盒反馈→debug 的完整环。
"""

from __future__ import annotations

import shutil

import pytest

from server.openrsi import EvoState, evolve, extract_code

_STUB = "def add(a, b):\n    raise NotImplementedError\n"
_TEST = (
    "import unittest\n"
    "from solution import add\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
    "        self.assertEqual(add(-1, 1), 0)\n\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
)


def _fake_llm(prompt: str) -> str:
    """按 prompt 里的 OPERATOR 段返回确定性代码。draft 写错, debug 写对。"""
    if "OPERATOR: draft" in prompt:
        return "```python\ndef add(a, b):\n    return a - b  # bug\n```"
    if "OPERATOR: debug" in prompt or "OPERATOR: crossover" in prompt:
        return "```python\ndef add(a, b):\n    return a + b\n```"
    # improve: 保持正确实现
    return "```python\ndef add(a, b):\n    return a + b\n```"


def test_extract_code_strips_fence():
    assert extract_code("blah ```python\nx = 1\n``` tail") == "x = 1\n"
    assert extract_code("no fence here") == "no fence here\n"


def test_evostate_is_hashable_and_ordered():
    s = EvoState.of({"b.py": "2", "a.py": "1"})
    assert s.files == (("a.py", "1"), ("b.py", "2"))
    assert hash(s) == hash(EvoState.of({"a.py": "1", "b.py": "2"}))


@pytest.mark.skipif(shutil.which("python3") is None, reason="沙盒需要 python3")
def test_evolution_solves_toy_task():
    workspace = {"solution.py": _STUB, "test_solution.py": _TEST}
    result = evolve(
        task="Implement add(a, b) returning the sum.",
        workspace_files=workspace,
        target_files=["solution.py"],
        llm=_fake_llm,
        n_branches=2,
        budget=8,
        max_depth=3,
        isolation="none",  # hermetic: 不依赖 unshare
    )

    assert result.solved, f"演化未通关: reward={result.best_reward} stats={result.stats}"
    # 通关判定 = 测试全绿 (solved), 复合分含 diff 惩罚故正确解也常 <1.0 但应较高
    assert result.best_reward >= 0.75
    # 最优解必须是修好的版本
    assert "a + b" in result.best_files["solution.py"]
    # 测试文件未被算子改写 (天然冻结)
    assert result.best_files["test_solution.py"] == _TEST
    # 轨迹里应同时存在"死亡分支"(draft 未通关) 与"成功分支"(solved) —— 供 Phase 3 归纳
    ops = {t["op"] for t in result.trajectory}
    assert "draft" in ops and "debug" in ops
    assert any(not t["solved"] for t in result.trajectory)
    assert any(t["solved"] for t in result.trajectory)
    # 未传 holdout → 守卫默认关闭, 不误报 overfit
    assert result.overfit is False
    assert result.holdout_reward is None


# holdout 反作弊: train 只查 (2,3)=5, holdout 查 (1,1)=2 —— 硬编码 return 5 过 train 挂 holdout
_TRAIN_ONLY = (
    "import unittest\n"
    "from solution import add\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
)
_HOLDOUT = (
    "import unittest\n"
    "from solution import add\n\n"
    "class H(unittest.TestCase):\n"
    "    def test_h(self):\n"
    "        self.assertEqual(add(1, 1), 2)\n"
)


@pytest.mark.skipif(shutil.which("python3") is None, reason="沙盒需要 python3")
def test_holdout_catches_reward_hacking():
    """算子硬编码 return 5 骗过可见 train 测试, holdout 应抓到过拟合。"""

    def hardcode_llm(prompt: str) -> str:
        return "```python\ndef add(a, b):\n    return 5  # hardcoded to visible test\n```"

    result = evolve(
        task="Implement add(a, b).",
        workspace_files={"solution.py": _STUB, "test_solution.py": _TRAIN_ONLY},
        target_files=["solution.py"],
        llm=hardcode_llm,
        n_branches=2,
        budget=6,
        max_depth=2,
        isolation="none",
        holdout_files={"test_holdout.py": _HOLDOUT},
    )
    assert result.solved  # train 上"通关" (硬编码骗过可见测试)
    assert result.overfit  # 但 holdout 抓到过拟合
    assert result.holdout_reward is not None and result.holdout_reward < 1.0
    assert result.stats["overfit"] is True


def test_holdout_name_clash_with_train_test_raises():
    """holdout 与 train 测试重名会覆盖丢覆盖 → 入口 fail-fast, 不静默漏报。"""
    import pytest as _pytest

    with _pytest.raises(ValueError, match="重名"):
        evolve(
            task="x",
            workspace_files={"solution.py": _STUB, "test_solution.py": _TRAIN_ONLY},
            target_files=["solution.py"],
            llm=_fake_llm,
            holdout_files={"test_solution.py": _HOLDOUT},  # 与 train 测试同名
            isolation="none",
        )


@pytest.mark.skipif(shutil.which("python3") is None, reason="沙盒需要 python3")
def test_holdout_passes_genuine_solution():
    """真正正确的实现 (a+b) 应同时过 train 与 holdout, 不被误标 overfit。"""
    result = evolve(
        task="Implement add(a, b) returning the sum.",
        workspace_files={"solution.py": _STUB, "test_solution.py": _TRAIN_ONLY},
        target_files=["solution.py"],
        llm=_fake_llm,  # draft 写错 a-b → debug 修成 a+b (泛化正确)
        n_branches=2,
        budget=8,
        max_depth=3,
        isolation="none",
        holdout_files={"test_holdout.py": _HOLDOUT},
    )
    assert result.solved
    assert result.overfit is False
    assert result.holdout_reward is not None
