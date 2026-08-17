"""#4 置信图谱 + 先查图钩子: graft 边置信标注 + tool_guard observe 策略。"""

from __future__ import annotations

from server.graft_context import GraftContext
from server.tool_guard import ToolGuard
from server.tool_guard_policies import install_default_tool_policies, prefer_code_graph_policy


# ── 置信图谱: graft 装配层 ──────────────────────────────────────────────
def test_render_edges_marks_inferred():
    gc = GraftContext()
    idx = {"uniq": {"a.py"}, "dup": {"a.py", "b.py"}}
    rendered = gc._render_edges(["uniq", "dup"], idx)
    assert "uniq" in rendered
    assert "dup ⚠inferred" in rendered
    assert "uniq ⚠inferred" not in rendered


def test_undefined_edge_is_inferred():
    gc = GraftContext()
    # external_call 不在索引 → 低置信
    assert "external_call ⚠inferred" in gc._render_edges(["external_call"], {"x": {"a.py"}})


def test_def_index_from_synced_trees():
    gc = GraftContext()
    gc.sync(
        {
            "a.py": "def dup():\n    pass\ndef uniq():\n    pass\n",
            "b.py": "def dup():\n    pass\n",
        }
    )
    idx = gc._def_index()
    assert idx.get("dup") == {"a.py", "b.py"}  # 重名跨模块
    assert idx.get("uniq") == {"a.py"}


def test_assemble_includes_legend_when_found():
    gc = GraftContext()
    gc.sync({"a.py": "def target():\n    pass\n"})
    block = gc.assemble(["target"])
    assert "target" in block
    assert "⚠inferred" in block  # 图例说明存在


# ── 先查图钩子: tool_guard observe 策略 ─────────────────────────────────
def test_policy_advises_on_structural_tools():
    assert prefer_code_graph_policy("grep", {}, "master")
    assert prefer_code_graph_policy("read_file_ast", {}, "master")
    assert prefer_code_graph_policy("hicode_run", {}, "master") is None


def test_policy_observe_mode_does_not_deny():
    guard = ToolGuard()
    install_default_tool_policies(guard)
    assert guard.has_policy("prefer_code_graph")
    # observe 模式: grep 不被拦截, 只落 observed 轨迹
    guard.check("grep", {"pattern": "x"}, source="master")  # 不 raise
    trail = guard.trail()
    assert trail and trail[-1]["decision"] == "allow"
    assert any(o.get("policy") == "prefer_code_graph" for o in (trail[-1].get("observed") or []))
