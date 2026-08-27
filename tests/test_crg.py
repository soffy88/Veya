"""code-review-graph 3O 复刻门禁 — 图谱原语 / 技能包 / 懒构建。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for lib in ("oprim", "omodul", "oservi", "obase", "oskill"):
    sys.path.insert(0, str(ROOT / "platform" / "3O" / lib))

from oprim import _code_review_graph as crg  # noqa: E402

# =========================================================================
# 原语 (mock CRG CLI)
# =========================================================================


def test_available_and_missing(monkeypatch):

    monkeypatch.setattr(crg, "crg_available", lambda: False)
    r = crg.graph_status()
    assert r["ok"] is False and "未安装" in r["error"]

    monkeypatch.setattr(crg, "crg_available", lambda: True)
    assert crg.crg_available() is True


def test_graph_query_unknown_type():
    r = crg.graph_query("nonsense", "x")
    assert r["ok"] is False and "未知查询类型" in r["error"]


def test_cli_bridge_mock(monkeypatch):
    """CLI 桥: JSON 输出归一 + 非 JSON 兜底。"""

    def fake_run(args, **kw):
        if args[0] == "status":
            return {"ok": True, "nodes": 12, "edges": 34}
        if args[0] == "query":
            return {"ok": True, "results": [{"name": "server.app"}]}
        return {"ok": True}

    monkeypatch.setattr(crg, "_run", fake_run)
    st = crg.graph_status()
    assert st["nodes"] == 12
    q = crg.graph_query("callers_of", "server.app")
    assert q["results"][0]["name"] == "server.app"


def test_graph_ensure_lazy(monkeypatch):
    """懒构建: 图空 → register + build; 有图 → ready。"""
    calls: list[str] = []
    monkeypatch.setattr(crg, "graph_status", lambda cwd="": {"ok": True, "nodes": 0})
    monkeypatch.setattr(
        crg, "graph_register", lambda path, alias="": calls.append("register") or {"ok": True}
    )
    monkeypatch.setattr(
        crg, "graph_build", lambda cwd="", incremental=True: calls.append("build") or {"ok": True}
    )

    r = crg.graph_ensure("/tmp/repo")
    assert r["ready"] is True
    assert calls == ["register", "build"]


def test_graph_ensure_ready_skips_build(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(crg, "graph_status", lambda cwd="": {"ok": True, "nodes": 100})
    monkeypatch.setattr(
        crg, "graph_build", lambda cwd="", incremental=True: calls.append("build") or {"ok": True}
    )
    r = crg.graph_ensure("/tmp/repo")
    assert r["ready"] is True
    assert calls == []


# =========================================================================
# 技能包
# =========================================================================


def test_skill_pack_manifest():
    manifest = json.loads(
        (ROOT / "templates" / "skills" / "code_review_graph" / "manifest.json").read_text()
    )
    assert manifest["name"] == "code_review_graph"
    assert "impact" in manifest["parameters"]["properties"]["action"]["enum"]
    assert (ROOT / "templates" / "skills" / "code_review_graph" / "run.py").exists()


def test_skill_main_dispatches(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "crg_skill", ROOT / "templates" / "skills" / "code_review_graph" / "run.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    monkeypatch.setattr(
        mod, "graph_query", lambda t, target, cwd="": {"ok": True, "type": t, "target": target}
    )
    r = mod.main("query", query_type="tests_for", target="auth.py")
    assert r["ok"] is True and r["type"] == "tests_for"

    r2 = mod.main("impact", target="server/app.py")
    assert r2["ok"] is False  # impact 未 mock → 真实 CLI 缺失? 不: graph_impact 未 mock 走真实
    # 未 mock 的 action 在无 CRG 环境 → 结构化错误 (不崩)
    assert "ok" in r2


def test_ledger_registered():
    from server.operator_ledger import code_review_ledger_summary

    s = code_review_ledger_summary()
    assert s[0]["name"] == "code_review_graph"
    assert s[0]["status"] == "registered"


# =========================================================================
# 语义搜索原语 (graph_search / graph_semantic_search)
# =========================================================================


def test_graph_search_args_and_normalize(monkeypatch):
    """search: query + kind/limit 透传 CLI; 输出含 results。"""
    captured: list[list[str]] = []

    def fake_run(args, **kw):
        captured.append(args)
        return {
            "ok": True,
            "status": "ok",
            "search_mode": "fts",
            "summary": "Found 3",
            "results": [
                {
                    "name": "llm_call",
                    "qualified_name": "veya/llm.py::llm_call",
                    "kind": "Function",
                    "file_path": "veya/llm.py",
                    "line_start": 157,
                    "line_end": 167,
                }
            ],
        }

    monkeypatch.setattr(crg, "_run", fake_run)
    r = crg.graph_search("llm_call", kind="Function", limit=5)
    assert captured == [["search", "llm_call", "--kind", "Function", "--limit", "5"]]
    assert r["results"][0]["qualified_name"] == "veya/llm.py::llm_call"


def test_graph_search_empty_query():
    r = crg.graph_search("   ")
    assert r["ok"] is False and "不能为空" in r["error"]


def test_graph_search_no_limit(monkeypatch):
    captured: list[list[str]] = []

    def fake_run(args, **kw):
        captured.append(args)
        return {"ok": True, "results": []}

    monkeypatch.setattr(crg, "_run", fake_run)
    crg.graph_search("query", kind=None, limit=None)
    assert captured == [["search", "query"]]


def test_graph_semantic_search_ensures_and_searches(monkeypatch):
    """语义搜索: ensure 图就绪 → search; ensure_embed 时先 embed。"""
    calls: list[str] = []

    def fake_ensure(cwd=""):
        calls.append("ensure")
        return {"ok": True, "ready": True}

    def fake_search(query, *, kind=None, limit=None, cwd=""):
        calls.append(f"search:{query}")
        return {"ok": True, "results": [{"name": "x"}]}

    def fake_embed(*, cwd="", provider="local"):
        calls.append("embed")
        return {"ok": True}

    monkeypatch.setattr(crg, "graph_ensure", fake_ensure)
    monkeypatch.setattr(crg, "graph_search", fake_search)
    monkeypatch.setattr(crg, "graph_embed", fake_embed)

    r = crg.graph_semantic_search("路由", ensure_embed=True)
    assert r["ok"] is True
    assert calls == ["ensure", "embed", "search:路由"]


def test_graph_semantic_search_skips_embed_by_default(monkeypatch):
    calls: list[str] = []

    def fake_ensure(cwd=""):
        calls.append("ensure")
        return {"ok": True, "ready": True}

    def fake_search(query, *, kind=None, limit=None, cwd=""):
        calls.append("search")
        return {"ok": True, "results": []}

    monkeypatch.setattr(crg, "graph_ensure", fake_ensure)
    monkeypatch.setattr(crg, "graph_search", fake_search)

    crg.graph_semantic_search("路由")
    assert calls == ["ensure", "search"]


def test_graph_semantic_search_ensure_failure(monkeypatch):
    def fake_ensure(cwd=""):
        return {"ok": False, "error": "图谱未构建"}

    monkeypatch.setattr(crg, "graph_ensure", fake_ensure)
    r = crg.graph_semantic_search("路由")
    assert r["ok"] is False and "未就绪" in r["error"]


def test_graph_embed_failure_does_not_block_search(monkeypatch):
    """embed 失败(缺依赖) → 语义搜索回退 FTS 不阻断。"""
    calls: list[str] = []

    def fake_ensure(cwd=""):
        return {"ok": True, "ready": True}

    def fake_embed(*, cwd="", provider="local"):
        return {"ok": False, "error": "embeddings 依赖未安装"}

    def fake_search(query, *, kind=None, limit=None, cwd=""):
        calls.append("search")
        return {"ok": True, "results": []}

    monkeypatch.setattr(crg, "graph_ensure", fake_ensure)
    monkeypatch.setattr(crg, "graph_embed", fake_embed)
    monkeypatch.setattr(crg, "graph_search", fake_search)

    r = crg.graph_semantic_search("路由", ensure_embed=True)
    assert r["ok"] is True and calls == ["search"]
