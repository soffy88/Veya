"""Mechanism tests for lazy credential probing.

Every probe here is synthetic and every clock is fake. The properties under test
are the ones that decide whether a working executor gets locked out: a provider
with no probe must never look invalid, a success must actually have happened, a
failure must not be retried in a loop, and nothing may be reported as VALID
without a completed call.
"""

from __future__ import annotations

import pytest

from veya.remote.credential_probe import (
    MAX_BACKOFF,
    CredentialProbeCache,
    ProbeOutcome,
    ProbeResult,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _cache(**kwargs) -> tuple[CredentialProbeCache, FakeClock]:
    clock = FakeClock()
    return CredentialProbeCache(clock=clock, **kwargs), clock


def _counting(outcome: ProbeOutcome) -> tuple[object, list[int]]:
    calls: list[int] = []

    def probe():
        calls.append(1)
        return outcome

    return probe, calls


# ── UNPROBABLE: the property that protects working executors ───────────────
def test_provider_without_a_probe_is_unprobable_not_invalid() -> None:
    cache, clock = _cache()
    result = cache.probe("opencode")
    assert result.outcome is ProbeOutcome.UNPROBABLE
    assert result.is_negative is False
    assert result.is_unusable is False
    assert cache.credential_valid("opencode") is None


def test_unprobable_does_not_start_backoff() -> None:
    """Nothing was learned, so a later-registered probe must still run."""
    cache, clock = _cache()
    cache.probe("opencode")
    assert cache.retry_after("opencode") is None

    calls: list[int] = []
    cache.register("opencode", lambda: calls.append(1) or ProbeOutcome.VALID)
    assert cache.probe("opencode").outcome is ProbeOutcome.VALID
    assert len(calls) == 1


def test_registering_later_is_not_blocked_by_a_stale_verdict() -> None:
    cache, clock = _cache(ttl=1.0)
    cache.probe("pi")
    calls: list[int] = []
    cache.register("pi", lambda: calls.append(1) or ProbeOutcome.AUTH_FAILURE)
    assert cache.probe("pi").outcome is ProbeOutcome.AUTH_FAILURE
    assert len(calls) == 1


# ── VALID requires a completed call ────────────────────────────────────────
def test_valid_maps_to_true() -> None:
    cache, clock = _cache()
    cache.register("opencode", lambda: ProbeOutcome.VALID)
    assert cache.probe("opencode").outcome is ProbeOutcome.VALID
    assert cache.credential_valid("opencode") is True


def test_unknown_outcome_never_becomes_valid() -> None:
    cache, clock = _cache()
    cache.register("x", lambda: ProbeOutcome.UNKNOWN)
    cache.probe("x")
    assert cache.credential_valid("x") is None


@pytest.mark.parametrize(
    "outcome",
    [
        ProbeOutcome.AUTH_FAILURE,
        ProbeOutcome.PROVIDER_UNAVAILABLE,
        ProbeOutcome.PROVIDER_CONFIGURATION_FAILURE,
    ],
)
def test_negative_outcomes_map_to_false(outcome: ProbeOutcome) -> None:
    cache, clock = _cache()
    cache.register("x", lambda: outcome)
    cache.probe("x")
    assert cache.credential_valid("x") is False


def test_a_raising_probe_is_unknown_not_valid() -> None:
    cache, clock = _cache()
    cache.register("x", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    result = cache.probe("x")
    assert result.outcome is ProbeOutcome.UNKNOWN
    assert cache.credential_valid("x") is None
    assert "RuntimeError" in result.detail


def test_a_probe_returning_junk_is_unknown() -> None:
    cache, clock = _cache()
    cache.register("x", lambda: "totally fine")  # type: ignore[return-value]
    assert cache.probe("x").outcome is ProbeOutcome.UNKNOWN


def test_probe_may_return_a_full_result() -> None:
    cache, clock = _cache()
    cache.register("x", lambda: ProbeResult(ProbeOutcome.AUTH_FAILURE, 0.0, "401"))
    result = cache.probe("x")
    assert result.outcome is ProbeOutcome.AUTH_FAILURE
    assert result.detail == "401"


# ── TTL: a good answer is reused, not re-fetched ───────────────────────────
def test_success_is_cached_for_the_ttl() -> None:
    cache, clock = _cache(ttl=900.0)
    probe, calls = _counting(ProbeOutcome.VALID)
    cache.register("opencode", probe)
    cache.probe("opencode")
    clock.advance(899.0)
    cache.probe("opencode")
    assert len(calls) == 1


def test_success_is_reprobed_after_the_ttl() -> None:
    cache, clock = _cache(ttl=900.0)
    probe, calls = _counting(ProbeOutcome.VALID)
    cache.register("opencode", probe)
    cache.probe("opencode")
    clock.advance(901.0)
    cache.probe("opencode")
    assert len(calls) == 2


def test_ttl_is_per_executor() -> None:
    cache, clock = _cache(ttl=900.0)
    a_probe, a_calls = _counting(ProbeOutcome.VALID)
    b_probe, b_calls = _counting(ProbeOutcome.AUTH_FAILURE)
    cache.register("a", a_probe)
    cache.register("b", b_probe)
    cache.probe("a")
    cache.probe("b")
    assert len(a_calls) == len(b_calls) == 1


# ── backoff: the expensive failure path must not spin ──────────────────────
def test_failure_is_not_retried_immediately() -> None:
    cache, clock = _cache(backoff=300.0)
    probe, calls = _counting(ProbeOutcome.AUTH_FAILURE)
    cache.register("x", probe)
    cache.probe("x")
    clock.advance(299.0)
    cache.probe("x")
    assert len(calls) == 1


def test_failure_is_retried_after_backoff() -> None:
    cache, clock = _cache(backoff=300.0)
    probe, calls = _counting(ProbeOutcome.AUTH_FAILURE)
    cache.register("x", probe)
    cache.probe("x")
    clock.advance(301.0)
    cache.probe("x")
    assert len(calls) == 2


def test_backoff_doubles_on_consecutive_failures() -> None:
    cache, clock = _cache(backoff=100.0, max_backoff=MAX_BACKOFF)
    probe, _ = _counting(ProbeOutcome.AUTH_FAILURE)
    cache.register("x", probe)
    cache.probe("x")
    assert cache.retry_after("x") == pytest.approx(100.0)

    clock.advance(101.0)
    cache.probe("x")
    assert cache.retry_after("x") == pytest.approx(200.0)

    clock.advance(201.0)
    cache.probe("x")
    assert cache.retry_after("x") == pytest.approx(400.0)


def test_backoff_is_capped() -> None:
    cache, clock = _cache(backoff=100.0, max_backoff=250.0)
    probe, _ = _counting(ProbeOutcome.AUTH_FAILURE)
    cache.register("x", probe)
    for _ in range(6):
        cache.probe("x")
        clock.advance(cache.retry_after("x") + 1.0)
    assert cache.retry_after("x") is not None
    assert cache.retry_after("x") <= 250.0


def test_a_success_resets_backoff() -> None:
    cache, clock = _cache(backoff=100.0)
    failing = [True]

    def probe():
        if failing[0]:
            return ProbeOutcome.AUTH_FAILURE
        return ProbeOutcome.VALID

    cache.register("x", probe)
    cache.probe("x")
    assert cache.retry_after("x") is not None

    failing[0] = False
    clock.advance(101.0)
    assert cache.probe("x").outcome is ProbeOutcome.VALID
    assert cache.retry_after("x") is None


def test_unknown_is_retried_rather_than_cached() -> None:
    """An inconclusive probe must not throttle the next attempt for a full TTL."""
    cache, clock = _cache(ttl=900.0)
    probe, calls = _counting(ProbeOutcome.UNKNOWN)
    cache.register("x", probe)
    cache.probe("x")
    clock.advance(1.0)
    cache.probe("x")
    assert len(calls) == 2


# ── force and forget ───────────────────────────────────────────────────────
def test_force_bypasses_a_cached_success() -> None:
    cache, clock = _cache(ttl=900.0)
    probe, calls = _counting(ProbeOutcome.VALID)
    cache.register("x", probe)
    cache.probe("x")
    cache.probe("x", force=True)
    assert len(calls) == 2


def test_force_bypasses_backoff() -> None:
    cache, clock = _cache(backoff=300.0)
    probe, calls = _counting(ProbeOutcome.AUTH_FAILURE)
    cache.register("x", probe)
    cache.probe("x")
    cache.probe("x", force=True)
    assert len(calls) == 2


def test_forget_clears_the_verdict() -> None:
    cache, clock = _cache()
    cache.register("x", lambda: ProbeOutcome.VALID)
    cache.probe("x")
    assert cache.credential_valid("x") is True
    cache.forget("x")
    assert cache.credential_valid("x") is None
    assert cache.peek("x") is None


def test_peek_never_probes() -> None:
    cache, clock = _cache()
    probe, calls = _counting(ProbeOutcome.VALID)
    cache.register("x", probe)
    assert cache.peek("x") is None
    assert len(calls) == 0


def test_has_probe_reports_registration_without_probing() -> None:
    cache, clock = _cache()
    assert cache.has_probe("x") is False
    cache.register("x", lambda: ProbeOutcome.VALID)
    assert cache.has_probe("x") is True


# ── registry integration ───────────────────────────────────────────────────
def test_registry_probe_updates_the_identity() -> None:
    """A probe verdict must reach the identity, not just the cache."""
    from veya.remote.credential_probe import ProbeOutcome as Outcome
    from veya.remote.executor_registry import ExecutorRegistry

    registry = ExecutorRegistry()
    before = registry.identity("opencode").credential_valid
    result = registry.probe_credential("opencode", force=True)
    assert result.outcome is Outcome.UNPROBABLE
    # No verified probe means no evidence, so the identity must not move.
    assert registry.identity("opencode").credential_valid == before


def test_registry_probe_never_marks_valid_without_a_probe() -> None:
    from veya.remote.executor_registry import ExecutorRegistry

    registry = ExecutorRegistry()
    registry.probe_credential("codex", force=True)
    assert registry.identity("codex").credential_valid is None
    assert registry.identity("codex").credential_proven is False


def test_registry_probe_rejects_an_unknown_executor() -> None:
    from veya.remote.executor_registry import ExecutorRegistry

    with pytest.raises(ValueError, match="not registered"):
        ExecutorRegistry().probe_credential("no-such-executor")


def test_registry_discovery_makes_no_network_call() -> None:
    """The import path must stay offline: real calls measured 12.7s-217.7s."""
    import subprocess
    import sys

    code = (
        "import sys; sys.path[:0]=['platform/3O'];"
        "from veya.remote.credential_probe import CredentialProbeCache;"
        "from veya.remote.executor_registry import get_executor_registry;"
        "c=get_executor_registry()._probes;"
        "assert c.peek('opencode') is None and not c.has_probe('opencode'), 'discovery probed';"
        "print('clean')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
    assert "clean" in out.stdout, out.stderr[-800:]


def test_registry_preserves_a_local_refutation_through_an_unprobable_probe() -> None:
    """pi is locally refuted; an unprobable probe must not resurrect it."""
    from veya.remote.executor_registry import ExecutorRegistry

    registry = ExecutorRegistry()
    assert registry.identity("pi").credential_valid is False
    registry.probe_credential("pi", force=True)
    assert registry.identity("pi").credential_valid is False


def test_a_completed_call_overrides_a_local_refutation() -> None:
    """A real success is stronger evidence than reading a file.

    The reverse direction must hold too: if local structure says False but an
    authenticated call actually succeeds, the call wins, because it is the only
    thing here that proves the credential works.
    """
    from veya.remote.credential_probe import ProbeOutcome as Outcome
    from veya.remote.executor_registry import ExecutorRegistry

    registry = ExecutorRegistry()
    assert registry.identity("pi").credential_valid is False
    registry._probes.register("pi", lambda: Outcome.VALID)
    registry.probe_credential("pi", force=True)
    assert registry.identity("pi").credential_valid is True
    assert registry.identity("pi").credential_proven is True
    assert registry.identity("pi").auth_state == "AUTHENTICATED"


def test_a_negative_probe_marks_the_identity_invalid() -> None:
    from veya.remote.credential_probe import ProbeOutcome as Outcome
    from veya.remote.executor_registry import ExecutorRegistry

    registry = ExecutorRegistry()
    registry._probes.register("codex", lambda: Outcome.AUTH_FAILURE)
    registry.probe_credential("codex", force=True)
    assert registry.identity("codex").credential_valid is False
    assert registry.identity("codex").auth_state == "INVALID"
