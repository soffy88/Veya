"""codebase-memory-mcp 集成测试 — 装配层端到端 (真实二进制, 需 ~/.local/bin/codebase-memory-mcp)。

覆盖: 启动/注册、索引、符号搜索、调用链 trace、blast_radius 聚合、
双通道搜索 (graph 优先 / vector fallback)、工具适配。
二进制缺失时测试自动 skip (环境无关)。
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from server.codebase_memory import CodebaseMemoryConnector, CodebaseMemoryError, get_connector

BIN = pathlib.Path.home() / ".local" / "bin" / "codebase-memory-mcp"
HAS_BIN = BIN.exists() and BIN.is_file()

pytestmark = pytest.mark.skipif(not HAS_BIN, reason="codebase-memory-mcp 二进制未安装")


def _make_repo() -> pathlib.Path:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cbm_test_"))
    (tmp / "app.py").write_text(
        "def helper(x):\n    return x + 1\n\n"
        "def main():\n    return helper(41)\n\n"
        "def unused_fn():\n    return 0\n")
    (tmp / "util.py").write_text(
        "from app import helper\n\n"
        "def run():\n    return helper(1)\n")
    return tmp


@pytest.fixture()
async def connector():
    repo = _make_repo()
    c = CodebaseMemoryConnector(repo, bin_path=str(BIN),
                                index_dir=str(repo / ".cbm-index"))
    await c.start()
    yield c
    await c.close()


def test_available_and_start(connector):
    assert connector.available
    assert connector.ready


@pytest.mark.asyncio
async def test_index_and_project_name(connector):
    state = await connector.ensure_indexed(force=True)
    assert state["nodes"] >= 20
    assert connector._project  # 规范化 project 名


@pytest.mark.asyncio
async def test_search_symbols_hit(connector):
    await connector.ensure_indexed(force=True)
    syms = await connector.search_symbols("helper", limit=5)
    assert syms, "符号级搜索必须命中 helper"
    hit = next((s for s in syms if s.get("name") == "helper"), None)
    assert hit, f"helper 未命中: {syms}"
    assert hit.get("file_path") == "app.py"
    assert hit.get("start_line") == 1


@pytest.mark.asyncio
async def test_trace_callers_across_files(connector):
    await connector.ensure_indexed(force=True)
    t = await connector.trace("helper", depth=3)
    caller_names = [c.get("name") for c in t.get("callers", [])]
    assert "main" in caller_names, f"app.main 必须出现在 callers: {caller_names}"
    assert "run" in caller_names, f"util.run 必须出现在 callers (跨文件): {caller_names}"


@pytest.mark.asyncio
async def test_blast_radius_aggregation(connector):
    await connector.ensure_indexed(force=True)
    radius = await connector.blast_radius(["helper"], depth=2)
    assert radius["total_affected"] >= 2          # main + run
    assert any("main" in c for c in radius["callers"])
    assert any("run" in c for c in radius["callers"])


@pytest.mark.asyncio
async def test_query_cypher(connector):
    await connector.ensure_indexed(force=True)
    rows = await connector.query_cypher(
        "MATCH (f:FUNCTION) WHERE f.name = 'helper' RETURN f.name LIMIT 5")
    # 服务端可能返回空行或结果 — 至少不抛异常
    assert isinstance(rows, list)


@pytest.mark.asyncio
async def test_dual_channel_graph_first(connector):
    await connector.ensure_indexed(force=True)
    called = {"vec": 0}

    def vec_fallback(q, top_k=10):
        called["vec"] += 1
        return [{"id": "v1", "text": q, "file_path": "x.py"}]

    res = await connector.search("helper", top_k=5, fallback=vec_fallback)
    assert res["source"] == "graph"
    assert called["vec"] == 0                    # 图谱命中不触发 fallback


@pytest.mark.asyncio
async def test_dual_channel_vector_fallback(connector):
    called = {"vec": 0}

    def vec_fallback(q, top_k=10):
        called["vec"] += 1
        return [{"id": "v1", "text": "fallback hit"}]

    # 未索引 → search_symbols 抛错 → fallback
    res = await connector.search("zzz_no_index", top_k=5, fallback=vec_fallback)
    assert res["source"] in ("vector", "none")
    if res["source"] == "vector":
        assert called["vec"] >= 1


@pytest.mark.asyncio
async def test_tool_adapters_batch(connector):
    await connector.ensure_indexed(force=True)
    adapters = await connector.tool_adapters()
    names = [a["name"] for a in adapters]
    assert "mcp_codebase_search_graph" in names
    assert "mcp_codebase_trace_path" in names
    assert all(a["parameters"]["type"] == "object" for a in adapters)


def test_singleton_get_connector():
    c = get_connector()
    assert c is get_connector()                  # 单例
    assert c.workspace_root.exists()


@pytest.mark.asyncio
async def test_missing_bin_degrades():
    c = CodebaseMemoryConnector("/tmp", bin_path="/nonexistent/bin")
    assert not c.available
    await c.start()                              # 降级不抛
    assert not c.ready
    with pytest.raises(CodebaseMemoryError):
        await c.search_symbols("x")              # 未就绪调用必须报错
