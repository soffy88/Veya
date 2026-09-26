"""D5D gates: ACP session <-> MCP capability delivery assembly.

Matrix with deterministic fakes (no live processes): new-session attach,
unsupported honesty, resume rebind/refresh, refresh diff + failure preserve,
multi-session sharing, close/teardown idempotency, route-cache projection,
resources honesty, skills deferral, D3/D9 boundaries, shutdown.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from server.acp_mcp_delivery import (
    AcpMcpDelivery,
    McpSourceDiscovery,
    get_acp_mcp_delivery,
    reset_acp_mcp_delivery,
)


class FakeBackend:
    """Duck-typed ACP transport (start/run/close, no policy)."""

    def __init__(self, session_id: str = "acp-sess-1"):
        self._session_id = session_id
        self.started = 0
        self.runs: list[str] = []
        self.closed = 0

    async def start_session(self) -> str:
        self.started += 1
        return self._session_id

    async def run(self, prompt: str) -> dict:
        self.runs.append(prompt)
        return {"ok": True, "output": f"reply:{prompt}"}

    async def close(self) -> None:
        self.closed += 1


def _source(
    name: str,
    tools: list[str] | None = None,
    resources: list[str] | None = None,
    version: str = "1",
    transport: str = "http",
) -> McpSourceDiscovery:
    return McpSourceDiscovery(
        server=name,
        transport=transport,
        tools=tuple({"name": tool} for tool in (tools or [])),
        resources=tuple({"uri": uri} for uri in (resources or [])),
        skills_supported=False,
        version=version,
    )


def _delivery(sources: dict[str, McpSourceDiscovery], **kwargs):
    async def discover(name: str) -> McpSourceDiscovery:
        if name not in sources:
            raise KeyError(f"unknown source {name!r}")
        found = sources[name]
        if isinstance(found, Exception):
            raise found
        return found

    return AcpMcpDelivery(discover=discover, **kwargs)


@pytest.fixture()
def events() -> list[tuple[str, dict]]:
    return []


def _emit(events: list[tuple[str, dict]]):
    def _record(topic: str, payload: dict) -> None:
        events.append((topic, payload))

    return _record


# -- new session ---------------------------------------------------------------


@pytest.mark.asyncio()
async def test_new_session_attach_supported(events):
    delivery = _delivery({"docs": _source("docs", ["read", "search"])}, emit=_emit(events))
    backend = FakeBackend()
    session_id = await backend.start_session()
    session = await delivery.open_session(
        session_id,
        backend_kind="acp",
        sources=["docs"],
        on_close_transport=backend.close,
    )
    assert session.sources["docs"].status == "attached"
    assert session.sources["docs"].tool_refs == ("mcp/docs/read", "mcp/docs/search")
    assert session.sources["docs"].generation == 1
    assert session.capabilities["mcp_attachment"] is True
    assert any(topic == "acp.mcp.attached" for topic, _ in events)


@pytest.mark.asyncio()
async def test_unsupported_source_no_fake_attach():
    delivery = _delivery({})
    session = await delivery.open_session("s-1", sources=["ghost"])
    assert session.sources["ghost"].status == "unsupported"
    assert session.sources["ghost"].tool_refs == ()
    assert session.closed is False


@pytest.mark.asyncio()
async def test_transport_mismatch_denied_closed():
    delivery = _delivery(
        {"db": _source("db", ["q"], transport="websocket")},
        emit=None,
    )
    session = await delivery.open_session("s-1", sources=["db"])
    assert session.sources["db"].status == "denied"
    assert "transport" in (session.sources["db"].error or "")


@pytest.mark.asyncio()
async def test_failed_discovery_preserves_session():
    async def boom(name: str):
        raise RuntimeError("transport down")

    delivery = AcpMcpDelivery(discover=boom)
    session = await delivery.open_session("s-1", sources=["db"])
    assert session.sources["db"].status == "failed"
    assert session.closed is False


# -- resume / refresh ---------------------------------------------------------------


@pytest.mark.asyncio()
async def test_resume_rebind_no_duplicate():
    delivery = _delivery({"docs": _source("docs", ["read"])})
    first = await delivery.open_session("s-1", sources=["docs"], reuse_key="b:cwd")
    second = await delivery.open_session("s-1b", sources=["docs"], reuse_key="b:cwd")
    assert second.session_id == first.session_id
    assert second.sources["docs"].generation == 1
    assert len(delivery.open_sessions()) == 1


@pytest.mark.asyncio()
async def test_resume_stale_triggers_refresh():
    sources = {"docs": _source("docs", ["read"], version="1")}
    delivery = _delivery(sources)
    await delivery.open_session("s-1", sources=["docs"], reuse_key="b:cwd")
    sources["docs"] = _source("docs", ["read", "write"], version="2")
    session = await delivery.reconnect("s-1")
    assert session.sources["docs"].generation == 2
    assert session.sources["docs"].tool_refs == ("mcp/docs/read", "mcp/docs/write")


@pytest.mark.asyncio()
async def test_refresh_diff_and_failure_preserves_last_good(events):
    sources = {"docs": _source("docs", ["read", "old"], version="1")}
    delivery = _delivery(sources, emit=_emit(events))
    await delivery.open_session("s-1", sources=["docs"])
    sources["docs"] = _source("docs", ["read", "new"], version="2")
    report = await delivery.refresh("s-1")
    assert report["refreshed"][0]["added"] == ["mcp/docs/new"]
    assert report["refreshed"][0]["removed"] == ["mcp/docs/old"]
    assert report["failed"] == []

    sources["docs"] = RuntimeError("boom")
    report = await delivery.refresh("s-1")
    assert report["failed"] and report["refreshed"] == []
    session = delivery.get_session("s-1")
    assert session.sources["docs"].generation == 2
    assert session.sources["docs"].tool_refs == ("mcp/docs/new", "mcp/docs/read")
    assert session.sources["docs"].status == "stale"
    assert any(topic == "acp.mcp.refresh_failed" for topic, _ in events)


# -- multi-session sharing ---------------------------------------------------------------


@pytest.mark.asyncio()
async def test_shared_source_close_safety():
    delivery = _delivery({"docs": _source("docs", ["read"])})
    await delivery.open_session("s-a", sources=["docs"])
    await delivery.open_session("s-b", sources=["docs"])
    assert delivery.source_users("docs") == ["s-a", "s-b"]
    await delivery.close_session("s-a")
    assert delivery.get_session("s-b").sources["docs"].status == "attached"
    assert delivery.source_users("docs") == ["s-b"]
    report = await delivery.close_session("s-b")
    assert report["closed"] is True
    assert delivery.source_users("docs") == []


@pytest.mark.asyncio()
async def test_double_close_idempotent():
    delivery = _delivery({"docs": _source("docs", ["read"])})
    backend = FakeBackend()
    session = await delivery.open_session("s-1", sources=["docs"], on_close_transport=backend.close)
    first = await delivery.close_session("s-1")
    assert first["already"] is False
    assert first["remote_close"]["supported"] is True
    assert backend.closed == 1
    second = await delivery.close_session("s-1")
    assert second["already"] is True
    assert backend.closed == 1
    assert session.closed is True


@pytest.mark.asyncio()
async def test_close_without_transport_hook_records_honestly():
    delivery = _delivery({"docs": _source("docs", ["read"])})
    await delivery.open_session("s-1", sources=["docs"])
    report = await delivery.close_session("s-1")
    assert report["remote_close"]["supported"] is False


# -- resources / skills -------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_resources_refs_attached_when_present():
    delivery = _delivery({"docs": _source("docs", ["read"], ["res://a"])})
    session = await delivery.open_session("s-1", sources=["docs"])
    assert session.sources["docs"].resource_refs == ("res://a",)
    refs = session.sources["docs"].capability_refs()
    assert {item.kind for item in refs} == {"tool", "resource"}
    assert all(item.kind != "skill" for item in refs)


def test_skills_deferred_no_fake_attachment():
    discovery = _source("docs", ["read"])
    assert discovery.skills_supported is False


# -- D3 / D9 boundaries ----------------------------------------------------------------------


def test_d3_boundary_resume_disposition_untouched():
    from runtime.execution.resume import ResumeDisposition, decide_resume_disposition

    decision = decide_resume_disposition(trigger="infra_retry", session_id="s", resume_capable=True)
    assert decision.disposition is ResumeDisposition.RESUME_SESSION
    import pathlib

    import server.acp_mcp_delivery as module

    source = pathlib.Path(module.__file__).read_text()
    assert "ResumeDisposition" not in source
    assert "decide_resume_disposition" not in source


def test_d9_boundary_no_ranking_or_routing():
    import pathlib

    import server.acp_mcp_delivery as module

    source = pathlib.Path(module.__file__).read_text().lower()
    for token in (
        "top_k",
        "topk",
        "ranking",
        "capabilityrouter",
        "capabilityprojection",
        "token_budget",
        "semantic rout",
    ):
        assert token not in source


# -- shutdown ----------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_close_all_detaches_everything():
    delivery = _delivery({"a": _source("a", ["t"]), "b": _source("b", ["t"])})
    await delivery.open_session("s-1", sources=["a"])
    await delivery.open_session("s-2", sources=["b"])
    report = await delivery.close_all()
    assert sorted(report["closed"]) == ["s-1", "s-2"]
    assert report["failed"] == []
    assert delivery.open_sessions() == []
    again = await delivery.close_all()
    assert again == {"closed": [], "failed": []}


def test_singleton_assembly():

    reset_acp_mcp_delivery()
    try:
        first = get_acp_mcp_delivery()
        assert get_acp_mcp_delivery() is first
    finally:
        reset_acp_mcp_delivery()


class _ProcCompatibleBackend:
    """ACPBackend-shaped double for the production _run_acp path."""

    instances: ClassVar[list[_ProcCompatibleBackend]] = []

    def __init__(self, command, *, agent="general", cwd=None, env=None):
        self.command = command
        self.started = 0
        self.runs: list[str] = []
        self.closed = 0
        _ProcCompatibleBackend.instances.append(self)

    async def start_session(self) -> str:
        self.started += 1
        return "acp-live-sess"

    async def run(self, prompt: str, **kwargs) -> dict:
        self.runs.append(prompt)
        return {"ok": True, "output": f"reply:{prompt}"}

    async def close(self) -> None:
        self.closed += 1


@pytest.mark.asyncio()
async def test_production_run_acp_attaches_and_runs(monkeypatch):
    import server.backends as backends_module

    _ProcCompatibleBackend.instances.clear()
    monkeypatch.setattr(backends_module, "ACPBackend", _ProcCompatibleBackend)
    reset_acp_mcp_delivery()
    try:
        registry = backends_module.BackendRegistry()
        registry.register("fake-acp", "acp", command=["true"])
        result = await registry.run("fake-acp", "hello", cwd="/tmp")
        assert result["ok"] is True
        assert "reply:hello" in result["output"]
        backend = _ProcCompatibleBackend.instances[-1]
        assert backend.started == 1
        assert backend.closed == 1
        delivery = get_acp_mcp_delivery()
        assert delivery.open_sessions() == ["acp-live-sess"]
        await delivery.close_session("acp-live-sess")
        assert backend.closed == 2
    finally:
        reset_acp_mcp_delivery()
