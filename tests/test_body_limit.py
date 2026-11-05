"""8MB request-body limit (Autoloop 1.1.1 graft).

Entry points used to read bodies fully with no limit. The shared helper
refuses anything over MAX_BODY_BYTES with 413 before signature checks or
parsing; the events ingest route and the channel webhook body parser both
read through it.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from server.routes.body_limit import MAX_BODY_BYTES, read_limited_body

pytestmark = pytest.mark.asyncio


class _FakeRequest:
    """Minimal stand-in for a Starlette request: headers + stream + _body."""

    def __init__(self, chunks, *, content_length=None):
        self._chunks = list(chunks)
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self.consumed = 0

    async def stream(self):
        for chunk in self._chunks:
            self.consumed += 1
            yield chunk


async def test_small_body_passes_and_caches():
    req = _FakeRequest([b"a", b"bc"])
    assert await read_limited_body(req) == b"abc"
    assert req._body == b"abc"


async def test_declared_length_over_limit_rejected_without_reading():
    req = _FakeRequest([b"x"], content_length=MAX_BODY_BYTES + 1)
    with pytest.raises(HTTPException) as excinfo:
        await read_limited_body(req)
    assert excinfo.value.status_code == 413
    assert req.consumed == 0


async def test_streaming_body_over_limit():
    req = _FakeRequest([b"x" * (MAX_BODY_BYTES // 2), b"y" * (MAX_BODY_BYTES // 2 + 1)])
    with pytest.raises(HTTPException) as excinfo:
        await read_limited_body(req)
    assert excinfo.value.status_code == 413


async def test_exact_limit_passes():
    req = _FakeRequest([b"x" * MAX_BODY_BYTES], content_length=MAX_BODY_BYTES)
    assert len(await read_limited_body(req)) == MAX_BODY_BYTES


async def test_bad_content_length_falls_through_to_stream():
    req = _FakeRequest([b"{}"], content_length="not-a-number")
    assert await read_limited_body(req) == b"{}"


async def test_json_body_helper():
    from server.routes.channels import _json_body

    assert await _json_body(_FakeRequest([b'{"a": 1}'])) == {"a": 1}
    assert await _json_body(_FakeRequest([])) == {}
    with pytest.raises(HTTPException) as excinfo:
        await _json_body(_FakeRequest([b"x" * (MAX_BODY_BYTES + 1)]))
    assert excinfo.value.status_code == 413


@pytest.fixture()
def events_client(monkeypatch):
    from server import auth as auth_mod
    from server.routes import events as events_mod
    from server.routes.events import router as events_router

    class _FakeHub:
        async def ingest(self, data, *, user=None):
            return {"status": "dispatched", "rule": "test"}

    app = FastAPI()
    app.dependency_overrides[auth_mod.require_user] = lambda: {"user_id": "u1"}
    monkeypatch.setattr(events_mod, "get_event_hub", lambda: _FakeHub())
    app.include_router(events_router)
    return TestClient(app)


def test_events_ingest_oversized_body_413(events_client):
    resp = events_client.post("/api/v1/events/ingest", content=b"x" * (MAX_BODY_BYTES + 1))
    assert resp.status_code == 413


def test_events_ingest_small_body_reaches_validation(events_client):
    # b"{}" passes the size guard, then fails envelope validation as 422 —
    # proving the small body was not rejected by the limit.
    resp = events_client.post("/api/v1/events/ingest", content=b"{}")
    assert resp.status_code == 422
