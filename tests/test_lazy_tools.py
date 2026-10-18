"""Two-stage lazy tool loading — opt-in, session-scoped, default off.

Every test here exercises the ``VEYA_LAZY_TOOLS=1`` path against an injected
registry/surface and asserts the three invariants the design promises:

1. disabled by default: the surface is byte-identical to the frozen full one;
2. discovery is model-triggered (``search_tools``), never programmatic;
3. growth is append-only, so an already-sent prefix never moves (prompt cache).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from server import lazy_tools
from server.coordinator_master import MasterCoordinator
from server.tool_registry import (
    MasterToolRegistry,
    SideEffect,
    _current_master_session,
    master_tools,
    register_lazy_search_tool,
)


class _HistoryStore:
    async def load(self, *_args: Any, **_kwargs: Any) -> list[dict]:
        return []

    async def save(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _MemoryStore:
    async def retrieve(self, *_args: Any, **_kwargs: Any) -> list[dict]:
        return []

    async def add(self, *_args: Any, **_kwargs: Any) -> str:
        return ""


def _coordinator(**kwargs: Any) -> MasterCoordinator:
    return MasterCoordinator(
        history_store=_HistoryStore(),
        memory_store=_MemoryStore(),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _lazy_state(monkeypatch):
    """Isolate the process-wide lazy state and the env flag for each test."""
    for name in (
        "VEYA_LAZY_TOOLS",
        "VEYA_LAZY_RESIDENT_TOOLS",
        "VEYA_TOOL_SCHEMA_METRICS",
    ):
        monkeypatch.delenv(name, raising=False)
    had_search_tool = master_tools.has("search_tools")
    lazy_tools.reset_sessions()
    yield
    lazy_tools.reset_sessions()
    # A coordinator built without an injected registry registers into the
    # process-global surface; undo it so this module never leaks the opt-in
    # discovery tool into an unrelated test module.
    if not had_search_tool and master_tools.has("search_tools"):
        master_tools.unregister("search_tools")
    monkeypatch.delenv("VEYA_LAZY_TOOLS", raising=False)
    monkeypatch.delenv("VEYA_LAZY_RESIDENT_TOOLS", raising=False)
    monkeypatch.delenv("VEYA_TOOL_SCHEMA_METRICS", raising=False)


def _names(schemas: list[dict]) -> list[str]:
    return [schema["function"]["name"] for schema in schemas]


def _make_coordinator(
    monkeypatch=None,
    *,
    tools: list[str] | None = None,
    enabled: bool = True,
) -> MasterCoordinator:
    """A coordinator whose surface is a small, deterministic injected registry."""
    if monkeypatch is not None:
        monkeypatch.setenv("VEYA_LAZY_TOOLS", "1" if enabled else "0")
        if tools is not None:
            monkeypatch.setenv("VEYA_LAZY_RESIDENT_TOOLS", ",".join(tools))
    return _coordinator(llm_fn=lambda *_a, **_k: None)


# ---------------------------------------------------------------------------
# 1. Default off == frozen full surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_surface_is_identical_to_frozen_full_surface() -> None:
    coord = _make_coordinator(enabled=False, tools=[])
    full = coord._raw_get_all_tool_schemas()
    assert coord.get_all_tool_schemas() == full
    assert "search_tools" not in _names(full)
    # The prompt path is untouched as well.
    assert coord.get_system_prompt() == coord._agent.get_system_prompt()


def test_disabled_does_not_register_discovery_tool() -> None:
    registry = MasterToolRegistry()
    registry.register("probe", "probe", {"type": "object", "properties": {}}, lambda: "ok")
    register_lazy_search_tool(registry) if lazy_tools.enabled() else None
    assert not registry.has("search_tools")


# ---------------------------------------------------------------------------
# 2. Resident surface + discovery tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lazy_surface_is_resident_plus_host_tools_only(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_LAZY_TOOLS", "1")
    monkeypatch.setenv("VEYA_LAZY_RESIDENT_TOOLS", "alpha,beta")
    registry = MasterToolRegistry()

    def _noop(*_a: Any, **_k: Any) -> str:
        return "ok"

    for name in ("alpha", "beta", "gamma", "delta"):
        registry.register(name, f"{name} tool", {"type": "object", "properties": {}}, _noop)

    coord = _coordinator(tools=registry, llm_fn=lambda *_a, **_k: None)
    assert coord._lazy_tools_enabled is True

    names = _names(coord.get_all_tool_schemas())
    # The engine's non-unloadable host tools keep their leading prefix, then the
    # resident set (search_tools first, force-injected by the resident list).
    assert names[: len(coord._system_tool_names)] == list(coord._system_tool_names)
    resident_block = names[len(coord._system_tool_names) :]
    assert resident_block[0] == "search_tools"
    assert {"alpha", "beta"} <= set(names)
    assert {"gamma", "delta"}.isdisjoint(names)
    assert registry.has("search_tools")


@pytest.mark.asyncio
async def test_search_tools_returns_full_schemas_and_loads_session(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_LAZY_TOOLS", "1")
    monkeypatch.setenv("VEYA_LAZY_RESIDENT_TOOLS", "alpha")
    registry = MasterToolRegistry()

    def _noop(*_a: Any, **_k: Any) -> str:
        return "ok"

    registry.register("alpha", "alpha tool", {"type": "object", "properties": {}}, _noop)
    registry.register(
        "crop_image",
        "Crop an image to the given pixel rectangle.",
        {
            "type": "object",
            "properties": {"box": {"type": "array", "items": {"type": "integer"}}},
            "required": ["box"],
        },
        _noop,
    )

    coord = _coordinator(tools=registry, llm_fn=lambda *_a, **_k: None)
    assert coord.tools is registry
    session = "s-search"
    token = _current_master_session.set(session)
    catalog_token = lazy_tools.bind_catalog(coord._raw_get_all_tool_schemas())
    try:
        raw = await registry.execute(
            "search_tools", {"query": "crop image rectangle pixels", "top_k": 3}
        )
    finally:
        lazy_tools.reset_catalog(catalog_token)
        _current_master_session.reset(token)

    payload = json.loads(raw)
    assert "crop_image" in payload["loaded"]
    # The returned schema is the complete registered function schema, not a summary.
    crop = next(fn for fn in payload["tools"] if fn["name"] == "crop_image")
    assert crop["parameters"]["required"] == ["box"]

    # ... and it is now part of this session's surface, appended after residents.
    token = _current_master_session.set(session)
    try:
        names = _names(coord.get_all_tool_schemas())
    finally:
        _current_master_session.reset(token)
    assert "crop_image" in names
    assert names.index("crop_image") > names.index("alpha")
    assert names.index("crop_image") > len(coord._system_tool_names)
    # A different session never inherits it.
    other = _current_master_session.set("s-other")
    try:
        assert "crop_image" not in _names(coord.get_all_tool_schemas())
    finally:
        _current_master_session.reset(other)


@pytest.mark.asyncio
async def test_loaded_tools_are_session_scoped(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_LAZY_TOOLS", "1")
    monkeypatch.setenv("VEYA_LAZY_RESIDENT_TOOLS", "alpha")
    registry = MasterToolRegistry()
    registry.register("alpha", "alpha", {"type": "object", "properties": {}}, lambda: "ok")
    registry.register("gamma", "gamma", {"type": "object", "properties": {}}, lambda: "ok")
    coord = _coordinator(tools=registry, llm_fn=lambda *_a, **_k: None)

    lazy_tools.load_tools("session-a", ("gamma",))
    assert "gamma" not in _names(coord.get_all_tool_schemas())

    token = _current_master_session.set("session-a")
    try:
        assert "gamma" in _names(coord.get_all_tool_schemas())
    finally:
        _current_master_session.reset(token)


# ---------------------------------------------------------------------------
# 3. Prefix stability (prompt-cache friendliness)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_growth_is_append_only_and_prefix_is_stable(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_LAZY_TOOLS", "1")
    monkeypatch.setenv("VEYA_LAZY_RESIDENT_TOOLS", "alpha,beta")
    registry = MasterToolRegistry()
    for name in ("alpha", "beta", "gamma", "delta"):
        registry.register(name, name, {"type": "object", "properties": {}}, lambda: "ok")
    coord = _coordinator(tools=registry, llm_fn=lambda *_a, **_k: None)

    token = _current_master_session.set("s-stable")
    try:
        before = _names(coord.get_all_tool_schemas())
        lazy_tools.load_tools("s-stable", ("gamma",))
        after_one = _names(coord.get_all_tool_schemas())
        lazy_tools.load_tools("s-stable", ("delta",))
        after_two = _names(coord.get_all_tool_schemas())
    finally:
        _current_master_session.reset(token)

    assert after_one[: len(before)] == before
    assert after_two[: len(after_one)] == after_one
    assert after_two[len(before) :] == ["gamma", "delta"]


# ---------------------------------------------------------------------------
# 4. Direct-call fallback (auto-load + execute, no error)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_call_to_unloaded_tool_loads_and_executes(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_LAZY_TOOLS", "1")
    monkeypatch.setenv("VEYA_LAZY_RESIDENT_TOOLS", "alpha")
    registry = MasterToolRegistry()
    calls: list[str] = []
    registry.register("alpha", "alpha", {"type": "object", "properties": {}}, lambda: "ok")
    registry.register(
        "write_note",
        "Write a note file.",
        {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
        lambda **kw: calls.append("write_note") or "written",
        side_effect=SideEffect.LOCAL_WRITE,
    )
    coord = _coordinator(tools=registry, llm_fn=lambda *_a, **_k: None)
    token = _current_master_session.set("s-fallback")
    try:
        # The model called a registered tool that was never loaded into the
        # visible surface — the guard must not raise, must execute it, and must
        # keep it loaded for later rounds.
        assert "write_note" not in _names(coord.get_all_tool_schemas())
        result = await coord.handle_tool_call("write_note", {"content": "hello"})
        assert result == "written"
        assert calls == ["write_note"]
        assert "write_note" in _names(coord.get_all_tool_schemas())
    finally:
        _current_master_session.reset(token)


@pytest.mark.asyncio
async def test_direct_call_to_unregistered_tool_still_errors(monkeypatch) -> None:
    """The fallback only covers real capabilities; hallucinated names must fail."""
    coord = _make_coordinator(monkeypatch, tools=[])
    token = _current_master_session.set("s-hallucination")
    try:
        with pytest.raises(Exception, match=r"not loaded|not a registered tool"):
            await coord.handle_tool_call("no_such_tool", {})
        # A hallucinated name never enters the session surface.
        assert "no_such_tool" not in _names(coord.get_all_tool_schemas())
    finally:
        _current_master_session.reset(token)


# ---------------------------------------------------------------------------
# 5. Compact system prompt instead of the full inventory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lazy_prompt_uses_compact_inventory(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_LAZY_TOOLS", "1")
    monkeypatch.setenv("VEYA_LAZY_RESIDENT_TOOLS", "alpha")
    registry = MasterToolRegistry()
    for name in ("alpha", "gamma"):
        registry.register(name, f"{name} tool", {"type": "object", "properties": {}}, lambda: "ok")
    coord = _coordinator(tools=registry, llm_fn=lambda *_a, **_k: None)

    monkeypatch.setattr(coord._agent.memory, "inject_subconscious", lambda: "")
    prompt = coord.get_system_prompt()
    assert "lazy loading enabled" in prompt
    # Resident tools are advertised by name; the full per-tool signature dump
    # that the frozen prompt renders is gone.
    assert "`alpha`" in prompt
    assert "`gamma`" not in prompt.split("All registered capabilities:")[0]
    assert "search_tools(query)" in prompt


# ---------------------------------------------------------------------------
# 6. Measurement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_compare_full_against_sent_surface(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_LAZY_TOOLS", "1")
    monkeypatch.setenv("VEYA_TOOL_SCHEMA_METRICS", "1")
    monkeypatch.setenv("VEYA_LAZY_RESIDENT_TOOLS", "alpha")
    registry = MasterToolRegistry()
    for name in ("alpha", "gamma", "delta"):
        registry.register(
            name,
            f"{name} does a moderately long described thing",
            {"type": "object", "properties": {"x": {"type": "string"}}},
            lambda: "ok",
        )
    coord = _coordinator(tools=registry, llm_fn=lambda *_a, **_k: None)

    token = _current_master_session.set("s-metrics")
    try:
        coord.get_all_tool_schemas()
        metrics = coord._lazy_schema_metrics
    finally:
        _current_master_session.reset(token)

    assert metrics["enabled"] is True
    assert metrics["full"]["tool_count"] > metrics["sent"]["tool_count"]
    assert metrics["full"]["chars"] > metrics["sent"]["chars"]
    assert metrics["full"]["estimated_tokens"] > metrics["sent"]["estimated_tokens"]
    assert metrics["saved_chars"] == metrics["full"]["chars"] - metrics["sent"]["chars"]


@pytest.mark.asyncio
async def test_no_metrics_without_flag() -> None:
    coord = _make_coordinator(enabled=False, tools=[])
    coord.get_all_tool_schemas()
    assert coord._lazy_schema_metrics == {}


def test_resident_override_always_keeps_discovery_tool(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_LAZY_RESIDENT_TOOLS", "custom_only")
    names = lazy_tools.resident_tool_names()
    assert names[0] == "search_tools"
    assert "custom_only" in names


def test_estimate_tokens_counts_cjk_aware() -> None:
    assert lazy_tools.estimate_tokens("沙箱") >= 2
    assert lazy_tools.estimate_tokens("") == 0


# ---------------------------------------------------------------------------
# 7. Global registry contract: discovery tool is never added implicitly
# ---------------------------------------------------------------------------


def test_global_registry_unchanged_without_flag() -> None:
    assert not master_tools.has("search_tools")


@pytest.mark.asyncio
async def test_session_eviction_clears_loaded_state(monkeypatch) -> None:
    coord = _make_coordinator(tools=[])
    lazy_tools.load_tools("s-evicted", ("gamma",))
    from server import auth

    first = auth._user_ctx.set({"user_id": "u1", "username": "u1"})
    try:
        coord._bind_history_owner("s-evicted")
        assert lazy_tools.loaded_tools("s-evicted") == ("gamma",)
    finally:
        auth._user_ctx.reset(first)
    second = auth._user_ctx.set({"user_id": "u2", "username": "u2"})
    try:
        coord._bind_history_owner("s-evicted")
    finally:
        auth._user_ctx.reset(second)
    assert lazy_tools.loaded_tools("s-evicted") == ()
