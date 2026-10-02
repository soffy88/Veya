"""L1 executor authority contract (spec Phase 4: P4.1-P4.4).

The point of these is that there is exactly one inventory of executors and one
thing that decides which of them may run. A parallel list drifts, and because
it is consulted as a lookup gate, drift shows up as a crash mid-dispatch rather
than a clean refusal.
"""

from __future__ import annotations

import pytest

from veya.remote.executor_health import resolve_executor
from veya.remote.executor_registry import (
    ExecutorAvailability,
    get_executor_registry,
    is_retired_executor,
)


@pytest.fixture
def registry():
    return get_executor_registry()


# ── P4.2 the record carries every contract field ────────────────────────


REQUIRED_FIELDS = {
    "name": "executor_id",
    "type": "executor_kind",
    "capabilities": "capabilities",
    "supported_operations": "supported_operations",
    "health": "health",
    "availability": "status",
    "priority": "priority",
    "provider": "provider",
    "failure_state": "failure_state",
    "last_seen": "updated_at",
}


@pytest.mark.parametrize("contract,field", sorted(REQUIRED_FIELDS.items()))
def test_executor_record_exposes_the_contract(registry, contract: str, field: str) -> None:
    identity = next(iter(registry.snapshot().values()))
    assert field in identity.to_dict(), f"{contract} is not reported"


def test_record_stays_layered_not_a_god_object(registry) -> None:
    """The four concerns must remain separately addressable."""

    identity = registry.identity("opencode")
    assert identity.identity.name == "opencode"
    assert identity.identity.provider == identity.provider
    assert isinstance(identity.capability.supported_operations, frozenset)
    assert identity.runtime.availability == identity.status
    assert identity.policy.priority == identity.priority


def test_availability_vocabulary_is_normalised(registry) -> None:
    allowed = {e.value for e in ExecutorAvailability}
    for identity in registry.snapshot().values():
        assert identity.availability in allowed, identity.executor_id


# ── P4.3 Hicode is legacy and can never be admitted ─────────────────────


def test_hicode_is_not_in_the_inventory(registry) -> None:
    assert "hicode" not in registry.snapshot()
    assert "hicode" not in registry.ordered_ids()


def test_hicode_is_marked_retired() -> None:
    assert is_retired_executor("hicode") is True


def test_hicode_identity_lookup_refuses(registry) -> None:
    with pytest.raises(ValueError, match="retired"):
        registry.identity("hicode")


@pytest.mark.parametrize("requested", ["hicode", "HICODE", " hicode "])
def test_hicode_is_never_selected(requested: str) -> None:
    with pytest.raises(ValueError):
        resolve_executor(requested=requested, explicit_pin=True)


def test_hicode_cannot_enter_via_registration(registry) -> None:
    from veya.remote.executor_registry import ExecutorRuntimeIdentity

    with pytest.raises(ValueError, match="retired"):
        registry.register(
            ExecutorRuntimeIdentity(
                executor_id="hicode",
                executor_kind="l1_worker",
                provider=None,
                model=None,
                auth_state="AUTHENTICATED",
                reachable=True,
                launcher="/usr/bin/hicode",
            )
        )
    assert "hicode" not in registry.snapshot()


# ── P4.4 selection is filtered, not ranked first ────────────────────────


def test_registered_without_capability_is_not_selectable(registry) -> None:
    """`acp` exists and is admitted, but has no capability record."""

    identity = registry.identity("acp")
    assert identity.capability.selectable is False
    assert identity.selectable is False


def test_unavailable_executor_is_not_selectable(registry) -> None:
    for identity in registry.snapshot().values():
        if identity.availability == str(ExecutorAvailability.UNAVAILABLE):
            assert identity.selectable is False, identity.executor_id


def test_unavailable_executor_is_never_chosen() -> None:
    """Selection must not hand back an executor that cannot run."""

    selected, _evidence = resolve_executor()
    registry = get_executor_registry()
    identity = registry.identity(selected)
    assert identity.availability != str(ExecutorAvailability.UNAVAILABLE)
    assert identity.capability.selectable is True


def test_priority_never_overrides_capability() -> None:
    """The highest-priority executor is skipped when it lacks capability."""

    with pytest.raises(ValueError, match="not ready"):
        resolve_executor(requested="acp", explicit_pin=True)


def test_omitted_worker_goes_through_the_registry() -> None:
    selected, _evidence = resolve_executor()
    assert selected in get_executor_registry().ordered_ids()
    assert selected != "hicode"


# ── P4.1 no second inventory ────────────────────────────────────────────


def test_worker_labels_come_from_the_registry() -> None:
    from veya.remote.tool_adapter import _WORKER_TYPES

    registry = get_executor_registry()
    for executor_id in registry.snapshot():
        assert executor_id in _WORKER_TYPES, executor_id
        assert _WORKER_TYPES[executor_id] == executor_id.upper()


def test_unknown_worker_label_fails_closed_with_context() -> None:
    from veya.remote.tool_adapter import _WORKER_TYPES

    with pytest.raises(KeyError, match="admitted"):
        _WORKER_TYPES["not-an-executor"]
