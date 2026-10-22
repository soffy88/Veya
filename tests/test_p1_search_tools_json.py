"""P1-2 — ``search_tools`` output must always be parseable JSON.

The registry applies ``max_result_chars`` as a hard character cut
(``_to_str``), which used to slice the payload mid-string whenever the hit set
carried a long schema (``browser_session``, ``world_intel``). The model then
received unparseable output and lazy discovery silently failed for those
queries.

These tests pin the new budget-aware assembly: full schemas when they fit,
fewer hits when they do not, and a compact schema (name + description +
required arguments) as the last resort.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from server import lazy_tools
from server.tool_registry import (
    MasterToolRegistry,
    _current_master_session,
    _search_tools_render,
    master_tools,
)


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setenv("VEYA_LAZY_TOOLS", "1")
    had_search_tool = master_tools.has("search_tools")
    lazy_tools.reset_sessions()
    yield
    lazy_tools.reset_sessions()
    if not had_search_tool and master_tools.has("search_tools"):
        master_tools.unregister("search_tools")
    monkeypatch.delenv("VEYA_LAZY_TOOLS", raising=False)


def _noop(*_a: Any, **_k: Any) -> str:
    return "ok"


def _long_schema(name: str, chars: int) -> dict:
    """A tool schema whose description is exactly ``chars`` characters long."""
    filler = "lorem ipsum dolor sit amet "
    description = (filler * (chars // len(filler) + 1))[:chars]
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "action": {"type": "string"},
                "args": {"type": "object"},
            },
            "required": ["session_id", "action"],
        },
    }


def _payload(hits: list[tuple[int, str, dict]]) -> dict:
    return {
        "loaded": [row[1] for row in hits],
        "session_loaded_count": len(hits),
        "tools": [row[2] for row in hits],
        "note": "These full schemas are now available on the next round.",
    }


def test_full_schemas_are_kept_when_they_fit():
    hits = [(5, "small_tool", _long_schema("small_tool", 200))]
    payload = _payload(hits)
    out = _search_tools_render(payload, hits)
    parsed = json.loads(out)
    assert parsed["tools"][0]["name"] == "small_tool"
    # The complete schema survives, parameters included.
    assert parsed["tools"][0]["parameters"]["required"] == ["session_id", "action"]


def test_long_hit_set_drops_trailing_hits_but_stays_valid_json():
    """A hit set over budget loses tail hits, never JSON validity."""
    hits = [(5, f"tool_{i}", _long_schema(f"tool_{i}", 6000)) for i in range(8)]
    payload = _payload(hits)
    out = _search_tools_render(payload, hits)
    parsed = json.loads(out)
    assert len(out) <= 16000
    assert len(parsed["tools"]) < len(hits)
    # Every dropped hit is still recorded as loaded for the session.
    assert parsed["loaded"] == [row[1] for row in hits]


def test_oversized_single_schema_falls_back_to_compact_form():
    """A schema that alone exceeds the budget compacts to name + description.

    Production hit this with a gateway tool whose description carries a 160
    action catalog; the compact form keeps the identity, purpose, and required
    arguments the model needs to decide on a call.
    """
    hits = [(5, "mcp_world_intel", _long_schema("mcp_world_intel", 60000))]
    payload = _payload(hits)
    out = _search_tools_render(payload, hits)
    parsed = json.loads(out)
    assert len(out) <= 16000
    assert len(parsed["tools"]) == 1
    entry = parsed["tools"][0]
    assert entry["name"] == "mcp_world_intel"
    # The description is bounded on a word boundary, never cut mid-string.
    assert entry["description"].endswith("…")
    assert entry["description"] != payload["tools"][0]["description"]
    # The compact form (name + required arguments) is the middle step; this
    # schema is large enough that even it exceeds the budget, so the payload
    # degrades to name + bounded description instead of invalid JSON.
    assert len(out) < len(json.dumps(payload["tools"][0], ensure_ascii=False))


def test_compact_form_keeps_required_arguments_when_it_fits():
    """A schema that fits the budget in compact form keeps its required args."""
    hits = [(5, "mcp_world_intel", _long_schema("mcp_world_intel", 30000))]
    payload = _payload(hits)
    out = _search_tools_render(payload, hits)
    parsed = json.loads(out)
    assert len(out) <= 16000
    compact = parsed["tools"][0]
    assert compact["name"] == "mcp_world_intel"
    assert compact["parameters"]["required"] == ["session_id", "action"]
    assert "reduced to name + description + required arguments" in parsed["note"]


def test_render_never_emits_unparseable_output():
    """Property check across hit counts and schema sizes."""
    for count in (1, 3, 8, 20):
        for size in (100, 2000, 9000, 60000):
            hits = [(5, f"t{i}", _long_schema(f"t{i}", size)) for i in range(count)]
            out = _search_tools_render(_payload(hits), hits)
            json.loads(out)  # must never raise
            assert len(out) <= 16000, (count, size, len(out))
            parsed = json.loads(out)
            assert parsed["tools"], "at least one hit is always returned"


def test_browser_session_query_returns_parseable_json(clean_registry):
    """End-to-end: the acceptance-report failure case, on the real registry."""
    registry = MasterToolRegistry()
    for name in ("crop_image", "resize_image", "blur_image"):
        registry.register(
            name,
            "Image editing tool for the browser session pipeline.",
            {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "action": {"type": "string"},
                    "args": {"type": "object"},
                },
                "required": ["session_id", "action"],
            },
            _noop,
        )
    # The oversized schema that used to break the payload.
    registry.register(
        "browser_session",
        "Persistent browser session: open, snapshot, click, type, close.",
        {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "action": {"type": "string"},
                "args": {"type": "object"},
                "element_ref": {"type": "integer"},
                "text": {"type": "string"},
            },
            "required": ["session_id", "action"],
        },
        _noop,
    )
    from server.tool_registry import register_lazy_search_tool

    register_lazy_search_tool(registry)

    token = _current_master_session.set("s-p1-2")
    catalog_token = lazy_tools.bind_catalog(
        [s for s in registry._schemas if s["function"]["name"] != "search_tools"]
    )
    try:
        raw = asyncio.run(
            registry.execute(
                "search_tools",
                {"query": "browser session open snapshot click type close", "top_k": 8},
            )
        )
    finally:
        lazy_tools.reset_catalog(catalog_token)
        _current_master_session.reset(token)

    payload = json.loads(raw)
    assert "browser_session" in payload["loaded"]
    assert any(tool["name"] == "browser_session" for tool in payload["tools"])


def test_search_tools_via_helper_is_json_safe(clean_registry, monkeypatch):
    """The registered tool path (with the registry's own result cap) stays JSON."""
    from server.tool_registry import register_lazy_search_tool

    registry = MasterToolRegistry()
    for i in range(6):
        registry.register(
            f"browser_step_{i}",
            "Drive a persistent browser session step by step.",
            {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "action": {"type": "string"},
                    "payload": {"type": "object"},
                },
                "required": ["session_id", "action"],
            },
            _noop,
        )
    register_lazy_search_tool(registry)
    token = _current_master_session.set("s-p1-2-helper")
    catalog_token = lazy_tools.bind_catalog(
        [s for s in registry._schemas if s["function"]["name"] != "search_tools"]
    )
    try:
        raw = asyncio.run(
            registry.execute("search_tools", {"query": "browser session step", "top_k": 8})
        )
    finally:
        lazy_tools.reset_catalog(catalog_token)
        _current_master_session.reset(token)

    json.loads(raw)
