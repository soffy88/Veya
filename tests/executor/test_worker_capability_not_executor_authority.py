"""WorkerRuntime capability authority boundary.

``WorkerCapabilities`` models runtime lifecycle/recovery behavior
(``supports_sleep``, ``supports_revive``, ``supports_context_rollover``,
``recovery_capability``). It is deliberately NOT an executor authority: it must
not decide admission, ordering, aliasing, retirement, or fallback.

``ExecutorIdentity.capabilities`` (static executor metadata tags) is a different
model from ``WorkerCapabilities``. Merging them would pollute both, so these
tests pin the separation.
"""

from __future__ import annotations

import inspect

import pytest

from veya.remote.executor_health import registry_order, resolve_executor
from veya.remote.executor_registry import get_executor_registry, is_retired_executor
from veya.remote.worker_runtime import (
    WORKER_CAPABILITIES,
    RecoveryCapability,
    WorkerCapabilities,
    capabilities_for,
)


def test_worker_capabilities_is_lifecycle_model() -> None:
    """WORKER_CAPABILITIES describes runtime lifecycle, not executor identity."""
    sample = next(iter(WORKER_CAPABILITIES.values()))
    assert isinstance(sample, WorkerCapabilities)
    fields = set(WorkerCapabilities.__dataclass_fields__)
    assert fields, "WorkerCapabilities must expose lifecycle fields"
    # Lifecycle concerns are present.
    assert fields & {
        "supports_sleep",
        "supports_revive",
        "supports_context_rollover",
        "recovery_capability",
    }
    assert isinstance(RecoveryCapability, type)


def test_worker_capabilities_does_not_control_admission() -> None:
    """Capability data cannot admit an executor the registry rejects."""
    registry = get_executor_registry()
    for executor_id in WORKER_CAPABILITIES:
        try:
            registry.identity(executor_id)
        except ValueError:
            # Present as capability data but not admitted: allowed, and it must
            # not appear in the routing universe derived from admission.
            assert executor_id not in registry_order()


def test_unknown_executor_cannot_become_admitted_via_capabilities_for() -> None:
    """capabilities_for() must not register, admit, or mutate the registry."""
    registry = get_executor_registry()
    before = dict(registry.snapshot())

    assert "zz_not_a_real_executor" not in WORKER_CAPABILITIES
    capabilities_for("zz_not_a_real_executor")

    assert dict(registry.snapshot()) == before
    assert "zz_not_a_real_executor" not in registry_order()


def test_capability_lookup_does_not_mutate_registry() -> None:
    """Reading capabilities is not an admission side effect."""
    registry = get_executor_registry()
    before = dict(registry.snapshot())
    for executor_id in registry_order():
        capabilities_for(executor_id)
    assert dict(registry.snapshot()) == before


def test_worker_capabilities_does_not_control_ordering() -> None:
    """Ordering comes from the registry, not from capability dict key order."""
    source = inspect.getsource(registry_order)
    assert "ordered_ids" in source
    # WORKER_CAPABILITIES may filter routability, but must not supply order.
    assert "sorted(" not in source


def test_worker_capabilities_does_not_control_fallback() -> None:
    """Fallback is qualification-driven; capability keys cannot nominate."""
    source = inspect.getsource(resolve_executor)
    assert "capable_candidates" in source


def test_worker_capabilities_gate_is_not_an_admission_authority() -> None:
    """Known limitation: resolve_executor still *reads* capability keys to gate requests.

    Today this is not a live divergence: every capability key is registry-admitted,
    so the gate and the registry agree. It is a second read of executor identity
    though, and retiring it needs its own change (an admitted-but-unroutable
    executor such as acp must reject rather than silently substitute).
    """
    from veya.remote.worker_runtime import WORKER_CAPABILITIES as _caps

    admitted = set(get_executor_registry().snapshot())
    assert set(_caps).issubset(admitted), "capability keys must stay a subset of admission"
    with pytest.raises(ValueError):
        resolve_executor(requested="acp")


def test_worker_capabilities_has_no_alias_authority() -> None:
    """Aliasing is registry-owned; capability data carries no alias mapping."""
    from veya.remote.executor_health import normalize_executor_name
    from veya.remote.executor_registry import normalize_executor_id

    for raw in ("agy", "claude-code", "opencode-go"):
        assert normalize_executor_name(raw) == normalize_executor_id(raw)


def test_worker_capabilities_has_no_retirement_authority() -> None:
    """Retirement is a registry decision, independent of capability data."""
    assert is_retired_executor("hicode") is True
    assert "hicode" not in WORKER_CAPABILITIES
    assert "hicode" not in registry_order()


def test_executor_registry_remains_admission_and_identity_authority() -> None:
    """The registry still owns WHO; WorkerRuntime only owns runtime behavior."""
    registry = get_executor_registry()
    assert set(registry_order()).issubset(set(registry.ordered_ids()))
    with pytest.raises(ValueError):
        registry.identity("unknown-agent")
    with pytest.raises(ValueError):
        registry.identity("hicode")
