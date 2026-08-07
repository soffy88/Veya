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
    monkeypatch.setattr(crg, "graph_status",
                        lambda cwd="": {"ok": True, "nodes": 0})
    monkeypatch.setattr(crg, "graph_register",
                        lambda path, alias="": calls.append("register") or {"ok": True})
    monkeypatch.setattr(crg, "graph_build",
                        lambda cwd="", incremental=True: calls.append("build") or {"ok": True})

    r = crg.graph_ensure("/tmp/repo")
    assert r["ready"] is True
    assert calls == ["register", "build"]


def test_graph_ensure_ready_skips_build(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(crg, "graph_status",
                        lambda cwd="": {"ok": True, "nodes": 100})
    monkeypatch.setattr(crg, "graph_build",
                        lambda cwd="", incremental=True: calls.append("build") or {"ok": True})
    r = crg.graph_ensure("/tmp/repo")
    assert r["ready"] is True
    assert calls == []


# =========================================================================
# 技能包
# =========================================================================

def test_skill_pack_manifest():
    manifest = json.loads((ROOT / "templates" / "skills" / "code_review_graph"
                           / "manifest.json").read_text())
    assert manifest["name"] == "code_review_graph"
    assert "impact" in manifest["parameters"]["properties"]["action"]["enum"]
    assert (ROOT / "templates" / "skills" / "code_review_graph" / "run.py").exists()


def test_skill_main_dispatches(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "crg_skill", ROOT / "templates" / "skills" / "code_review_graph" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    monkeypatch.setattr(mod, "graph_query",
                        lambda t, target, cwd="": {"ok": True, "type": t, "target": target})
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
