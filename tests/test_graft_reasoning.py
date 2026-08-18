"""Phase 1 (Graft 上下文) + Phase 3 (ReasoningBank 归纳) 装配验证。"""

from __future__ import annotations

from server.graft_context import GraftContext, extract_entities
from server.reasoning_bank import Experience, ReasoningBank

_AUTH = "def verify_token(tok):\n    return _decode(tok)\n\ndef _decode(tok):\n    return tok\n"
_HANDLER = "from auth import verify_token\n\ndef login(req):\n    return verify_token(req.token)\n"


# ------------------------------------------------------------------ Phase 1


def test_graft_find_traces_callers_and_callees():
    g = GraftContext()
    g.sync({"auth.py": _AUTH, "handler.py": _HANDLER})
    hits = g.find("verify_token")
    assert hits, "应定位到 verify_token"
    h = hits[0]
    assert h.module == "auth.py"
    assert "login" in h.callers  # 上游爆炸半径
    assert "_decode" in h.callees  # 下游依赖


def test_graft_incremental_cache_skips_unchanged():
    g = GraftContext()
    files = {"auth.py": _AUTH, "handler.py": _HANDLER}
    g.sync(files)
    stats = g.sync(files)  # 内容未变
    assert set(stats.cached) == set(files)
    assert stats.rebuilt == []
    files["auth.py"] = _AUTH + "\ndef extra():\n    pass\n"
    stats2 = g.sync(files)
    assert stats2.rebuilt == ["auth.py"]
    assert "handler.py" in stats2.cached


def test_graft_assemble_and_entity_extraction():
    g = GraftContext()
    g.sync({"auth.py": _AUTH, "handler.py": _HANDLER})
    block = g.assemble(["verify_token"])
    assert "Graft dependency map" in block
    assert "auth.py:1" in block
    assert "blast radius" in block
    ents = extract_entities("Refactor the verify_token flow in auth")
    assert "verify_token" in ents
    assert "the" not in ents  # 停用词过滤


# ------------------------------------------------------------------ Phase 3


def _fake_induction_llm(prompt: str) -> str:
    return (
        'Here is the lesson: {"situation": "refactoring token auth", '
        '"pitfall": "returning a - b style off-by-one bugs", '
        '"fix": "add regression tests before editing"} done'
    )


def test_reasoning_bank_induces_and_retrieves(tmp_path):
    bank = ReasoningBank(base_dir=tmp_path)
    trajectory = [
        {
            "op": "root",
            "reward": 0.0,
            "solved": False,
            "files": {"solution.py": "stub"},
            "feedback": "",
        },
        {
            "op": "draft",
            "reward": 0.3,
            "solved": False,
            "files": {"solution.py": "def add(a,b): return a-b"},
            "feedback": "1/2 failed",
        },
        {
            "op": "debug",
            "reward": 0.95,
            "solved": True,
            "files": {"solution.py": "def add(a,b): return a+b"},
            "feedback": "",
        },
    ]
    exp = bank.induce("Implement add", trajectory, _fake_induction_llm, target="solution.py")
    assert exp is not None
    assert "token auth" in exp.situation
    assert bank.count() == 1

    lessons = bank.search_experience("token auth refactor", k=3)
    assert lessons and isinstance(lessons[0], Experience)
    block = bank.as_rule_block(lessons)
    assert "RULE CONSTRAINTS" in block
    assert "instead" in block


def test_induce_anchors_on_near_miss_not_worst(tmp_path):
    """决策边界信用分配: 归纳锚定到 near-miss (差一点就过), 而非全局最差的垃圾分支。"""
    captured = {"prompt": ""}

    def capturing_llm(prompt: str) -> str:
        captured["prompt"] = prompt
        return '{"situation": "s", "pitfall": "p", "fix": "f"}'

    trajectory = [
        {
            "op": "draft",
            "reward": 0.05,
            "solved": False,
            "files": {"s.py": "GARBAGE_TOTAL_FAILURE"},
            "feedback": "syntax error",
        },
        {
            "op": "debug",
            "reward": 0.9,
            "solved": False,
            "files": {"s.py": "NEAR_MISS_SUBTLE_BUG"},
            "feedback": "1 test failed",
        },
        {
            "op": "improve",
            "reward": 0.98,
            "solved": True,
            "files": {"s.py": "CORRECT_SOLUTION"},
            "feedback": "",
        },
    ]
    bank = ReasoningBank(base_dir=tmp_path)
    exp = bank.induce("t", trajectory, capturing_llm, target="s.py")
    assert exp is not None
    assert "NEAR_MISS_SUBTLE_BUG" in captured["prompt"]
    assert "GARBAGE_TOTAL_FAILURE" not in captured["prompt"]
    assert "CORRECT_SOLUTION" in captured["prompt"]


def test_induce_needs_both_death_and_success():
    bank = ReasoningBank(base_dir=tmp_path_factory_dir())
    # 只有成功分支 (solved), 无死亡分支对照 → 不归纳
    traj = [
        {"op": "draft", "reward": 0.95, "solved": True, "files": {"x.py": "ok"}, "feedback": ""}
    ]
    assert bank.induce("t", traj, _fake_induction_llm) is None


def tmp_path_factory_dir():
    import tempfile

    return tempfile.mkdtemp(prefix="rb-")
