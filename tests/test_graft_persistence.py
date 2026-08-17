"""磁盘持久化缓存 + Runtime Call Tracing + 死代码检测 (code-graph-rag/graft 内化补点)。"""

from __future__ import annotations

from server.graft_context import GraftContext, trace_runtime_calls

_AUTH = "def entry():\n    used()\n\ndef used():\n    pass\n\ndef unused():\n    pass\n"


# ── 磁盘持久化缓存 ────────────────────────────────────────────────────
def test_disk_cache_loaded_by_new_instance(tmp_path):
    cache_path = tmp_path / "graft_cache.json"
    gc1 = GraftContext(cache_path=cache_path)
    gc1.sync({"a.py": _AUTH})
    assert cache_path.exists()

    gc2 = GraftContext(cache_path=cache_path)  # 新实例, 冷启动读盘
    hits = gc2.find("entry")  # 未调用 sync 也能命中: 缓存来自磁盘
    assert hits and hits[0].module == "a.py"


def test_disk_cache_survives_restart_and_skips_reparse(tmp_path):
    cache_path = tmp_path / "graft_cache.json"
    GraftContext(cache_path=cache_path).sync({"a.py": _AUTH})

    gc2 = GraftContext(cache_path=cache_path)
    stats = gc2.sync({"a.py": _AUTH})  # 内容未变, 磁盘缓存里的哈希应命中
    assert stats.cached == ["a.py"]
    assert stats.rebuilt == []


# ── Runtime Call Tracing 补盲 ─────────────────────────────────────────
def test_merge_runtime_edges_supplements_static_gap(tmp_path):
    gc = GraftContext(cache_path=tmp_path / "cache.json")
    gc.sync({"a.py": "def dispatcher():\n    pass\ndef dynamic_target():\n    pass\n"})
    assert "dynamic_target" not in gc._all_callees("dispatcher")  # 反射派发, 静态看不到

    gc.merge_runtime_edges({("dispatcher", "dynamic_target")})
    assert "dynamic_target" in gc._all_callees("dispatcher")
    assert "dispatcher" in gc._all_callers("dynamic_target")


def test_trace_runtime_calls_captures_real_edges():
    def callee():
        return 1

    def caller():
        return callee()

    result, edges = trace_runtime_calls(caller)
    assert result == 1
    assert ("caller", "callee") in edges


# ── 死代码检测 ────────────────────────────────────────────────────────
def test_dead_code_finds_unreached_function(tmp_path):
    gc = GraftContext(cache_path=tmp_path / "cache.json")
    gc.sync({"a.py": _AUTH})
    dead = gc.dead_code(["entry"])
    assert "unused" in dead
    assert "entry" not in dead
    assert "used" not in dead


def test_dead_code_reachable_only_via_runtime_edge_is_not_dead(tmp_path):
    gc = GraftContext(cache_path=tmp_path / "cache.json")
    gc.sync({"a.py": "def entry():\n    pass\ndef reflectively_called():\n    pass\n"})
    assert "reflectively_called" in gc.dead_code(["entry"])
    gc.merge_runtime_edges({("entry", "reflectively_called")})
    assert "reflectively_called" not in gc.dead_code(["entry"])
