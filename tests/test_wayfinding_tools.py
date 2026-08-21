"""server.wayfinding_tools 测试 — Wayfinding/StatefulProcedure 的 MasterAgent 工具面。

覆盖: chart→ticket→frontier→claim→resolve→complete→compile_runbook→
stateful_start→goto→history 的端到端链路, 以及各步骤的失败分支返回可读
错误 (不抛异常)。
"""

from __future__ import annotations

import re

import pytest
from obase.fs import FS

from server.wayfinding_tools import (
    stateful_current,
    stateful_goto,
    stateful_history,
    stateful_start,
    wayfind_add_fog,
    wayfind_add_ticket,
    wayfind_chart,
    wayfind_claim,
    wayfind_compile_runbook,
    wayfind_complete,
    wayfind_decisions,
    wayfind_frontier,
    wayfind_graduate_fog,
    wayfind_resolve,
    wayfind_rule_out_of_scope,
    wayfind_wire_blocking,
)


def _map_id(out: str) -> str:
    m = re.search(r"map_id=([a-f0-9]+)", out)
    assert m, out
    return m.group(1)


def _ticket_id(out: str) -> str:
    m = re.search(r"ticket ([a-f0-9]+)", out)
    assert m, out
    return m.group(1)


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("THREE_O_WAYFINDING_DIR", str(tmp_path / "wayfinding"))
    FS.set_default_working_dir(tmp_path / "obase_work")
    yield
    FS.reset_working_dir()


@pytest.mark.asyncio
async def test_chart_returns_map_id():
    out = await wayfind_chart("选下一代消息队列", notes="偏好开源")
    assert "✅" in out
    assert _map_id(out)


@pytest.mark.asyncio
async def test_add_ticket_and_frontier():
    map_id = _map_id(await wayfind_chart("d1"))
    out = await wayfind_add_ticket(map_id, "Kafka or NATS?", "compare throughput/ops cost")
    assert "✅" in out
    frontier = await wayfind_frontier(map_id)
    assert "Kafka or NATS?" in frontier


@pytest.mark.asyncio
async def test_claim_conflict_is_readable_not_an_exception():
    map_id = _map_id(await wayfind_chart("d1"))
    ticket_id = _ticket_id(await wayfind_add_ticket(map_id, "Q", "question"))
    r1 = await wayfind_claim(map_id, ticket_id, claimed_by="session-a")
    assert "✅" in r1
    r2 = await wayfind_claim(map_id, ticket_id, claimed_by="session-b")
    assert "✅" not in r2
    assert "session-a" in r2 or "失败" in r2


@pytest.mark.asyncio
async def test_wire_blocking_hides_ticket_from_frontier():
    map_id = _map_id(await wayfind_chart("d1"))
    a = _ticket_id(await wayfind_add_ticket(map_id, "A", "qa"))
    b = _ticket_id(await wayfind_add_ticket(map_id, "B", "qb"))
    await wayfind_wire_blocking(map_id, a, b)
    frontier = await wayfind_frontier(map_id)
    assert "A" in frontier and "B" not in frontier


@pytest.mark.asyncio
async def test_resolve_requires_claim_first():
    map_id = _map_id(await wayfind_chart("d1"))
    ticket_id = _ticket_id(await wayfind_add_ticket(map_id, "Q", "question"))
    out = await wayfind_resolve(map_id, ticket_id, resolution="did it", gist="short")
    assert "✅" not in out


@pytest.mark.asyncio
async def test_full_resolve_appears_in_decisions():
    map_id = _map_id(await wayfind_chart("d1"))
    ticket_id = _ticket_id(await wayfind_add_ticket(map_id, "Q", "question"))
    await wayfind_claim(map_id, ticket_id)
    out = await wayfind_resolve(map_id, ticket_id, resolution="did it", gist="picked postgres")
    assert "✅" in out
    decisions = await wayfind_decisions(map_id)
    assert "picked postgres" in decisions


@pytest.mark.asyncio
async def test_rule_out_of_scope_removes_from_frontier():
    map_id = _map_id(await wayfind_chart("d1"))
    ticket_id = _ticket_id(await wayfind_add_ticket(map_id, "Q", "question"))
    out = await wayfind_rule_out_of_scope(map_id, ticket_id, "not this cycle")
    assert "✅" in out
    frontier = await wayfind_frontier(map_id)
    assert "为空" in frontier


@pytest.mark.asyncio
async def test_add_fog_then_graduate_creates_ticket():
    map_id = _map_id(await wayfind_chart("d1"))
    await wayfind_add_fog(map_id, "auth strategy unclear")
    out = await wayfind_graduate_fog(map_id, "auth strategy unclear", ["OAuth vs session"])
    assert "✅" in out
    frontier = await wayfind_frontier(map_id)
    assert "OAuth vs session" in frontier


@pytest.mark.asyncio
async def test_complete_reports_remaining_before_clear():
    map_id = _map_id(await wayfind_chart("d1"))
    await wayfind_add_ticket(map_id, "Q", "question")
    out = await wayfind_complete(map_id)
    assert "✅" not in out
    assert "frontier 剩" in out


@pytest.mark.asyncio
async def test_end_to_end_chart_to_stateful_goto():
    map_id = _map_id(await wayfind_chart("ship feature X"))
    t1 = _ticket_id(await wayfind_add_ticket(map_id, "pick db", "which db?"))
    await wayfind_claim(map_id, t1)
    await wayfind_resolve(map_id, t1, resolution="chose postgres", gist="use postgres")

    complete_out = await wayfind_complete(map_id)
    assert "✅" in complete_out

    compiled = await wayfind_compile_runbook(map_id)
    assert "✅" in compiled

    started = await stateful_start(map_id, run_id="e2e-run")
    assert "✅" in started
    m = re.search(r"当前节点=(\S+)", started)
    assert m
    first_node = m.group(1)

    cur = await stateful_current(map_id, "e2e-run")
    assert first_node in cur

    goto_out = await stateful_goto(
        map_id, "e2e-run", "handoff", confirm_items=["Decision 'pick db' applied / verified"]
    )
    assert "✅" in goto_out

    history = await stateful_history("e2e-run")
    assert "handoff" in history


@pytest.mark.asyncio
async def test_stateful_goto_without_confirm_items_stays_blocked():
    map_id = _map_id(await wayfind_chart("d1"))
    t1 = _ticket_id(await wayfind_add_ticket(map_id, "pick db", "which db?"))
    await wayfind_claim(map_id, t1)
    await wayfind_resolve(map_id, t1, resolution="x", gist="y")
    await wayfind_complete(map_id)
    started = await stateful_start(map_id, run_id="unconfirmed-run")
    m = re.search(r"当前节点=(\S+)", started)
    assert m
    out = await stateful_goto(map_id, "unconfirmed-run", "handoff")
    assert "✅" not in out
    assert "unconfirmed items" in out


@pytest.mark.asyncio
async def test_stateful_goto_unknown_target_is_readable_error():
    map_id = _map_id(await wayfind_chart("d1"))
    t1 = _ticket_id(await wayfind_add_ticket(map_id, "pick db", "which db?"))
    await wayfind_claim(map_id, t1)
    await wayfind_resolve(map_id, t1, resolution="x", gist="y")
    await wayfind_complete(map_id)
    await stateful_start(map_id, run_id="bad-goto")
    out = await stateful_goto(map_id, "bad-goto", "nowhere")
    assert "✅" not in out
