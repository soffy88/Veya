"""W12 Part B — Brain circuit breaker (flag VEYA_BRAIN_BREAKER=1).

All offline. No network, no real providers.
"""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import ROOT  # noqa: F401 — path bootstrap


@pytest.fixture
def breaker_on(monkeypatch):
    """Enable the brain-breaker flag and reset state per test."""
    from veya.obase import brain_breaker as bb

    monkeypatch.setenv("VEYA_BRAIN_BREAKER", "1")
    bb.reset()
    yield bb
    bb.reset()


@pytest.fixture
def breaker_off(monkeypatch):
    """Disable the brain-breaker flag (the default)."""
    from veya.obase import brain_breaker as bb

    monkeypatch.setenv("VEYA_BRAIN_BREAKER", "0")
    bb.reset()
    yield bb
    bb.reset()


def _quota_error(status: int = 429, text: str = "429 Too Many Requests") -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://example.test/v1/chat/completions")
    resp = httpx.Response(status, request=req, text=text)
    return httpx.HTTPStatusError("quota", request=req, response=resp)


def test_classify_quota_error():
    from veya.obase import brain_breaker as bb

    assert bb.classify_quota_error("429 GoUsageLimitError limitName=monthly") == "monthly"
    assert bb.classify_quota_error("daily quota exhausted") == "daily"
    assert bb.classify_quota_error("429 Too Many Requests") == "plain"
    assert bb.classify_quota_error("server error 500") == ""
    assert bb.classify_quota_error("") == ""


def test_monthly_429_opens_breaker(breaker_on):
    bb = breaker_on
    assert not bb.breaker_open("opencode-go")
    exc = _quota_error(429, "429 GoUsageLimitError limitName=monthly")
    bb.record_provider_failure("opencode-go", exc)
    assert bb.breaker_open("opencode-go")
    state = bb.breaker_state("opencode-go")
    assert state["state"] == "open"
    assert state["last_kind"] == "monthly"
    assert state["repeats"] == 1


def test_breaker_state_no_secrets(breaker_on):
    bb = breaker_on
    exc = _quota_error(429, "429 GoUsageLimitError limitName=monthly")
    bb.record_provider_failure("opencode-go", exc)
    snap = bb.snapshot()
    assert len(snap) == 1
    provider_state = snap[0]
    assert provider_state["provider"] == "opencode-go"
    assert set(provider_state) <= {
        "provider", "state", "opened_at", "reopen_at", "repeats", "last_kind", "reason"
    }


def test_same_kind_repeats_doubles_cooldown(breaker_on):
    """Two consecutive plain-429s: repeats 1→3, cooldown doubles 600s→2400s."""
    bb = breaker_on
    now = 1_000_000.0
    exc = _quota_error(429, "429 Too Many Requests")
    bb.record_provider_failure("nim", exc, now=now)
    first = bb.breaker_state("nim", now=now)
    assert first["repeats"] == 1
    assert first["last_kind"] == "plain"
    assert first["reopen_at"] == pytest.approx(now + 600.0)

    # second consecutive failure of the same kind: repeats 1→3
    bb.record_provider_failure("nim", exc, now=now)
    second = bb.breaker_state("nim", now=now)
    assert second["repeats"] == 3
    # cooldown = 600 * 2^(3-1) = 2400s
    assert second["reopen_at"] == pytest.approx(now + 2400.0)


def test_cooldown_capped_at_two_hours(breaker_on):
    """Plain-429 cooldown never exceeds 2 h."""
    bb = breaker_on
    now = 1_000_000.0
    exc = _quota_error(429, "429 Too Many Requests")
    for _ in range(10):
        bb.record_provider_failure("nim", exc, now=now)
    state = bb.breaker_state("nim", now=now)
    assert (state["reopen_at"] - now) <= 7200.0


def test_breaker_open_respects_cooldown_expiry(breaker_on):
    bb = breaker_on
    now = 1_000_000.0
    exc = _quota_error(429, "429 Too Many Requests")
    bb.record_provider_failure("nim", exc, now=now)
    state = bb.breaker_state("nim", now=now)
    reopen_at = state["reopen_at"]
    assert bb.breaker_open("nim", now=now)
    assert not bb.breaker_open("nim", now=reopen_at + 1)
    assert bb.breaker_state("nim", now=reopen_at + 1)["state"] == "half_open"


def test_success_closes_breaker(breaker_on):
    bb = breaker_on
    exc = _quota_error(429, "429 Too Many Requests")
    bb.record_provider_failure("opencode-go", exc)
    assert bb.breaker_open("opencode-go")
    bb.note_provider_success("opencode-go")
    assert not bb.breaker_open("opencode-go")
    assert bb.breaker_state("opencode-go") is None


def test_all_tiers_open(breaker_on):
    bb = breaker_on
    exc = _quota_error(429, "429 Too Many Requests")
    assert not bb.all_tiers_open(["a", "b"])
    bb.record_provider_failure("a", exc)
    assert not bb.all_tiers_open(["a", "b"])
    bb.record_provider_failure("b", exc)
    assert bb.all_tiers_open(["a", "b"])


def test_flag_off_is_inert(breaker_off):
    bb = breaker_off
    exc = _quota_error(429, "429 Too Many Requests")
    bb.record_provider_failure("opencode-go", exc)
    assert not bb.breaker_open("opencode-go")
    assert bb.snapshot() == []
    assert not bb.all_tiers_open(["opencode-go"])
    bb.note_provider_success("opencode-go")  # no-op, must not raise


def test_non_quota_error_does_not_open(breaker_on):
    bb = breaker_on
    req = httpx.Request("POST", "https://example.test/v1/chat/completions")
    resp = httpx.Response(500, request=req, text="internal error")
    exc = httpx.HTTPStatusError("boom", request=req, response=resp)
    bb.record_provider_failure("opencode-go", exc)
    assert not bb.breaker_open("opencode-go")
