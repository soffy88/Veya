"""Real executor qualification (spec P4.8).

The inventory is read from `get_executor_registry().snapshot()` and nothing
else: a worker-type literal or a CLI worker table would let this pass while the
authority disagreed with itself.

Selection outcomes are asserted by shape, not by one hardcoded string, so the
contract survives a rename while still refusing a KeyError.
"""

from __future__ import annotations

import pytest

from veya.remote.executor_health import classify_executor_failure, resolve_executor
from veya.remote.executor_registry import (
    ExecutorAvailability,
    get_executor_registry,
    is_retired_executor,
)
from veya.remote.models import ExecutorFailureClass


@pytest.fixture
def registry():
    return get_executor_registry()


# ── 4.8.1 inventory comes from the authority ───────────────────────────


def test_inventory_matches_the_registry(registry) -> None:
    from veya.remote.tool_adapter import _WORKER_TYPES

    snapshot = set(registry.snapshot())
    assert snapshot, "registry is empty"
    # the derived label view can never hide or invent an executor
    assert set(iter(_WORKER_TYPES)) == snapshot


def test_every_admitted_executor_reports_the_contract(registry) -> None:
    allowed = {e.value for e in ExecutorAvailability}
    for identity in registry.snapshot().values():
        assert identity.availability in allowed, identity.executor_id
        assert identity.policy.priority >= 0


def test_registered_without_capability_is_not_selectable(registry) -> None:
    """`acp` exists and cannot run; that is a state, not a crash."""

    identity = registry.identity("acp")
    assert identity.capability.selectable is False
    assert identity.selectable is False


def test_priority_never_overrides_unavailability(registry) -> None:
    for identity in registry.snapshot().values():
        if identity.availability == str(ExecutorAvailability.UNAVAILABLE):
            assert identity.selectable is False, identity.executor_id


# ── 4.8.2 selection path fails closed, never with a KeyError ───────────


def test_executor_without_capability_is_refused_with_a_reason() -> None:
    with pytest.raises(ValueError) as excinfo:
        resolve_executor(requested="acp", explicit_pin=True)
    assert "not ready" in str(excinfo.value)


def test_unregistered_executor_is_refused() -> None:
    with pytest.raises(ValueError):
        resolve_executor(requested="unknown_executor", explicit_pin=True)


def test_retired_executor_is_refused_everywhere(registry) -> None:
    assert is_retired_executor("hicode")
    assert "hicode" not in registry.snapshot()
    with pytest.raises(ValueError):
        registry.identity("hicode")
    with pytest.raises(ValueError):
        resolve_executor(requested="hicode", explicit_pin=True)


def test_omitted_worker_selects_through_the_registry() -> None:
    selected, _evidence = resolve_executor()
    assert selected in get_executor_registry().ordered_ids()
    assert selected != "hicode"


# ── 4.8.4 failure boundaries stay in their own layer ──────────────────


@pytest.mark.parametrize(
    "detail,expected",
    [
        # a provider policy refusal is a provider fault, not a worker crash
        (
            'exit_code=1; stderr=400: {"code":400,"message":"User location is not '
            'supported for the API use.","status":"FAILED_PRECONDITION"}',
            ExecutorFailureClass.PROVIDER_CONFIGURATION_FAILURE,
        ),
        # a quota wall is not an outage
        (
            "429 usage_limit_reached: The usage limit has been reached",
            ExecutorFailureClass.PROVIDER_RATE_LIMIT,
        ),
        ("503 service unavailable", ExecutorFailureClass.PROVIDER_UNAVAILABLE),
        (
            "timeout_kind=INACTIVITY_TIMEOUT; inactivity_timeout_ms=300000",
            ExecutorFailureClass.PROVIDER_TIMEOUT,
        ),
        # genuinely local faults stay local
        ("segfault", ExecutorFailureClass.WORKER_CRASH),
        ("command not found", ExecutorFailureClass.ENVIRONMENT_FAILURE),
    ],
)
def test_failure_is_attributed_to_its_own_layer(detail: str, expected) -> None:
    assert classify_executor_failure(exit_code=1, detail=detail) is expected


def test_provider_failure_is_never_reported_as_an_executor_fault() -> None:
    for detail in (
        '400 {"status":"FAILED_PRECONDITION","message":"User location is not supported"}',
        "429 rate limit exceeded",
        "503 upstream connect error",
    ):
        result = classify_executor_failure(exit_code=1, detail=detail)
        assert not str(result).startswith("EXECUTOR_"), (detail, result)


def test_executor_crash_stays_an_executor_fault() -> None:
    assert classify_executor_failure(exit_code=-11, detail="segfault") is (
        ExecutorFailureClass.WORKER_CRASH
    )


# ── 4.8.5 the receipt carries identity, never secrets ────────────────


def test_registry_record_holds_no_credential_or_handle(registry) -> None:
    for identity in registry.snapshot().values():
        payload = identity.to_dict()
        for key in payload:
            lowered = key.lower()
            assert not any(
                token in lowered
                for token in (
                    "key",
                    "token",
                    "secret",
                    "password",
                    "credential",
                    "env",
                    "handle",
                    "pid",
                )
            ), f"{identity.executor_id} exposes {key}"
        # a launcher is a path, not a live handle
        assert identity.launcher is None or isinstance(identity.launcher, str)


def test_receipt_exposes_executor_identity(registry) -> None:
    identity = registry.identity("opencode")
    payload = identity.to_dict()
    for field in ("executor_id", "provider", "health", "status", "priority", "failure_state"):
        assert field in payload, field
