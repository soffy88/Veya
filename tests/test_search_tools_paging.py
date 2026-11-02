"""``search_tools`` pagination: oversized result sets are readable page by page.

Upstream's answer to an oversized result is a read-the-rest tool. Here the
ranking is deterministic for a given query and catalog, so ``offset`` pages
through the matches with no server-side result storage: each page loads only
its own tools, and ``total_matches``/``offset`` say where the page sits.
"""

from __future__ import annotations

import json

import pytest

from server import lazy_tools
from server.tool_registry import (
    MasterToolRegistry,
    _current_master_session,
    _lazy_search_tools,
    register_lazy_search_tool,
)


def _schema(name: str, chars: int = 60) -> dict:
    filler = "widget capability "
    description = (filler * (chars // len(filler) + 1))[:chars]
    return {
        "type": "function",
        "name": name,
        "description": f"Widget {name}: {description}",
        "parameters": {
            "type": "object",
            "properties": {"box": {"type": "string"}},
            "required": ["box"],
        },
    }


@pytest.fixture
def fake_catalog(monkeypatch):
    catalog = [{"function": _schema(f"w{i}_widget")} for i in range(1, 6)]
    monkeypatch.setattr(lazy_tools, "current_catalog", lambda: list(catalog))
    lazy_tools.reset_sessions()
    yield catalog
    lazy_tools.reset_sessions()


def _names(payload: dict) -> list[str]:
    return [tool["name"] for tool in payload["tools"]]


def test_offset_zero_matches_call_without_offset(fake_catalog):
    plain = json.loads(_lazy_search_tools("widget", top_k=2))
    paged = json.loads(_lazy_search_tools("widget", top_k=2, offset=0))
    assert paged == plain
    assert paged["total_matches"] == 5
    assert paged["offset"] == 0


def test_pages_cover_all_matches_without_overlap(fake_catalog):
    seen: list[str] = []
    for offset in (0, 2, 4):
        payload = json.loads(_lazy_search_tools("widget", top_k=2, offset=offset))
        assert payload["total_matches"] == 5
        assert payload["offset"] == offset
        seen.extend(_names(payload))
    assert seen == [f"w{i}_widget" for i in range(1, 6)]


def test_next_page_note_points_at_following_offset(fake_catalog):
    payload = json.loads(_lazy_search_tools("widget", top_k=2, offset=0))
    assert "3 more match(es)" in payload["note"]
    assert "offset=2" in payload["note"]
    last = json.loads(_lazy_search_tools("widget", top_k=2, offset=4))
    assert "more match(es)" not in last["note"]


def test_offset_beyond_total_is_empty_but_valid(fake_catalog):
    payload = json.loads(_lazy_search_tools("widget", top_k=2, offset=99))
    assert payload["tools"] == []
    assert payload["loaded"] == []
    assert payload["total_matches"] == 5
    assert payload["offset"] == 99
    assert "No further matches" in payload["note"]


def test_negative_offset_clamps_to_zero(fake_catalog):
    payload = json.loads(_lazy_search_tools("widget", top_k=2, offset=-3))
    assert payload["offset"] == 0
    assert _names(payload) == ["w1_widget", "w2_widget"]


def test_each_page_loads_only_its_tools(fake_catalog):
    session = "s-page"
    token = _current_master_session.set(session)
    try:
        _lazy_search_tools("widget", top_k=2, offset=0)
        assert lazy_tools.loaded_tools(session) == ("w1_widget", "w2_widget")
        _lazy_search_tools("widget", top_k=2, offset=2)
        assert lazy_tools.loaded_tools(session) == (
            "w1_widget",
            "w2_widget",
            "w3_widget",
            "w4_widget",
        )
    finally:
        _current_master_session.reset(token)


def test_budget_dropped_page_hits_stay_loaded(fake_catalog, monkeypatch):
    big = [{"function": _schema(f"big{i}_widget", chars=6000)} for i in range(3)]
    monkeypatch.setattr(lazy_tools, "current_catalog", lambda: list(big))
    out = _lazy_search_tools("widget", top_k=3)
    assert len(out) <= 16000
    payload = json.loads(out)
    assert payload["loaded"] == ["big0_widget", "big1_widget", "big2_widget"]
    assert len(payload["tools"]) < 3


@pytest.mark.asyncio
async def test_offset_flows_through_registry_execute(monkeypatch, fake_catalog):
    monkeypatch.setenv("VEYA_LAZY_TOOLS", "1")
    registry = MasterToolRegistry()
    register_lazy_search_tool(registry)
    session = "s-exec-page"
    token = _current_master_session.set(session)
    try:
        raw = await registry.execute(
            "search_tools", {"query": "widget", "top_k": 2, "offset": 2}
        )
    finally:
        _current_master_session.reset(token)
    payload = json.loads(raw)
    assert payload["offset"] == 2
    assert _names(payload) == ["w3_widget", "w4_widget"]
