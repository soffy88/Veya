"""SvelteKit /api/v1 proxy must forward the caller's credentials.

Regression guard: the proxy previously forwarded only `content-type`, which
silently turned every logged-in Web request into an anonymous one (401/403) and
buffered SSE responses so live events never reached the browser.
"""

from __future__ import annotations

from pathlib import Path

PROXY = Path("apps/web/src/routes/api/v1/[...path]/+server.ts").read_text(encoding="utf-8")


def test_proxy_authorization_forward():
    assert 'event.request.headers.get("authorization")' in PROXY
    assert '"authorization"' in PROXY


def test_proxy_cookie_forward():
    assert 'event.request.headers.get("cookie")' in PROXY
    assert '"cookie"' in PROXY


def test_proxy_does_not_forward_arbitrary_headers():
    """Only credentials are copied — no blanket header passthrough."""
    assert "event.request.headers.forEach" not in PROXY
    assert "new Headers(event.request.headers)" not in PROXY
    assert "...event.request.headers" not in PROXY
    for sensitive in ("x-api-key", "x-opencode-session", "proxy-authorization"):
        assert sensitive not in PROXY.lower(), sensitive


def test_proxy_streams_sse_unbuffered():
    assert "text/event-stream" in PROXY
    assert "upstream.body" in PROXY
    assert '"x-accel-buffering": "no"' in PROXY
