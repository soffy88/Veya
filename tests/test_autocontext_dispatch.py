"""主脑级自动上下文注入 + evolve_solution 真实派发路径 (master_tools.execute) 验证。"""

from __future__ import annotations

import asyncio

import pytest

import server.graft_autocontext as ac
from server.reasoning_bank import Experience, ReasoningBank

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
    if "reusable lesson" in prompt:
        return (
            '{"situation": "editing arithmetic helpers", "pitfall": "sign flips", '
            '"fix": "sandbox the unit tests first"}'
        )
    return "```python\ndef add(a, b):\n    return a + b\n```"


@pytest.fixture(autouse=True)
def _isolate_autocontext(tmp_path, monkeypatch):
    """把自动上下文的单例指向临时库/工作区, 不污染 ~/.veya, 保证 hermetic。"""
    from server.graft_explain import GraftExplainCache

    ac._graft = None
    ac._bank = ReasoningBank(base_dir=tmp_path / "bank")
    ac._mtime_cache = {}
    ac._explain_cache = GraftExplainCache(cache_path=tmp_path / "explain_cache.json")
    monkeypatch.setattr(ac, "_get_bank", lambda: ac._bank)
    monkeypatch.setattr(ac, "_get_explain_cache", lambda: ac._explain_cache)
    monkeypatch.setattr(ac, "_workspace_root", lambda: tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    yield
    ac._graft = None
    ac._bank = None
    ac._explain_cache = None


# ------------------------------------------------------------------ 自动上下文


def test_autocontext_default_off_is_noop_without_force(monkeypatch):
    monkeypatch.delenv("VEYA_GRAFT_CONTEXT", raising=False)
    assert ac.enabled() is False
    assert ac.build_block("please refactor verify_token") == ""


def test_autocontext_empty_is_noop():
    # 无历史规则、prompt 无可定位实体 → 完全空 (零行为变化)
    assert ac.build_block("hello how are you", force=True) == ""


def test_autocontext_injects_code_map(tmp_path):
    ws = ac._workspace_root()
    (ws / "auth.py").write_text(
        "def verify_token(tok):\n    return _decode(tok)\n\ndef _decode(t):\n    return t\n"
    )
    (ws / "handler.py").write_text(
        "from auth import verify_token\n\ndef login(r):\n    return verify_token(r.tok)\n"
    )
    block = ac.build_block("please refactor verify_token", force=True)
    assert block.startswith(ac.MARK)
    assert "Graft dependency map" in block
    assert "login" in block  # 爆炸半径


def test_autocontext_injects_learned_rules():
    ac._bank.store(
        Experience(
            situation="refactoring auth tokens",
            pitfall="forgetting cache invalidation",
            fix="clear the redis cache",
            task="auth",
        )
    )
    block = ac.build_block("refactoring auth tokens again", force=True)
    assert "RULE CONSTRAINTS" in block
    assert "redis" in block


def test_assemble_code_context_via_master_execute(tmp_path):
    from server.tool_registry import master_tools

    ws = ac._workspace_root()
    (ws / "auth.py").write_text("def verify_token(tok):\n    return tok\n")
    out = asyncio.run(master_tools.execute("assemble_code_context", {"query": "verify_token"}))
    assert "verify_token" in out or "No code-map" in out


# ------------------------------------------------ 讲解层 (graft_explain 内化)


async def _fake_explain_module(*, module, source, symbol_names, cache, **kwargs):
    return f"讲解: {module} 定义了 {', '.join(symbol_names)}"


def test_assemble_code_context_includes_narrative_layer(monkeypatch):
    monkeypatch.setattr("server.graft_explain.explain_module", _fake_explain_module)
    ws = ac._workspace_root()
    (ws / "auth.py").write_text("def verify_token(tok):\n    return tok\n")

    out = asyncio.run(ac.assemble_code_context("refactor verify_token"))

    assert "CODE EXPLANATION" in out
    assert "讲解: auth.py" in out
    assert "Graft dependency map" in out  # 结构化部分照旧存在, 讲解层是叠加不是替代


def test_assemble_code_context_narrative_failure_keeps_structural_block(monkeypatch):
    """讲解层 LLM 失败(抛异常)时, 结构化地图部分依然完整返回, 不受牵连。"""

    async def boom(**kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr("server.graft_explain.explain_module", boom)
    ws = ac._workspace_root()
    (ws / "auth.py").write_text("def verify_token(tok):\n    return tok\n")

    out = asyncio.run(ac.assemble_code_context("refactor verify_token"))

    assert "Graft dependency map" in out
    assert "CODE EXPLANATION" not in out


def test_assemble_code_context_no_match_has_no_narrative_section():
    out = asyncio.run(ac.assemble_code_context("hello how are you"))
    assert "CODE EXPLANATION" not in out


# --------------------------------------------- evolve_solution 真实派发路径


def test_evolve_solution_via_master_execute(tmp_path):
    """走主脑真实派发 master_tools.execute (过 tool_guard), 而非直调函数。"""
    from server.tool_registry import master_tools

    (tmp_path / "solution.py").write_text(_STUB)
    (tmp_path / "test_solution.py").write_text(_TEST)

    out = asyncio.run(
        master_tools.execute(
            "evolve_solution",
            {
                "task": "Implement add(a, b)",
                "target_file": "solution.py",
                "workspace_root": str(tmp_path),
                "n_branches": 2,
                "budget": 8,
                "_llm": _fake_llm,
                "_bank_dir": str(tmp_path / "bank2"),
            },
        )
    )
    assert "solved" in out.lower()
    assert "a + b" in (tmp_path / "solution.py").read_text()
