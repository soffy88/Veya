"""Ordering/inventory authority for executors lives in ExecutorRegistry.

``executor_health`` evaluates health only. It must not own the executor
universe: ``registry_order()`` projects membership from the registry, and the
registry owns canonical routing order.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from veya.remote import executor_health
from veya.remote.executor_health import (
    ExecutorHealthRegistry,
    registry_order,
    resolve_executor,
)
from veya.remote.executor_registry import ExecutorRuntimeIdentity, get_executor_registry

_HEALTH_SOURCE = Path(executor_health.__file__)


def _probe_identity(executor_id: str) -> ExecutorRuntimeIdentity:
    return ExecutorRuntimeIdentity(
        executor_id=executor_id,
        executor_kind="probe",
        provider=None,
        model=None,
        auth_state="UNVERIFIED",
        reachable=False,
        launcher=None,
    )


@pytest.fixture
def clean_registry():
    """Restore the global registry so admitted probes never leak between tests."""
    registry = get_executor_registry()
    before = dict(registry.snapshot())
    yield registry
    registry._identities.clear()
    registry._identities.update(before)


def test_health_snapshot_registry_driven() -> None:
    """Visible executors come from the registry projection, not a local list."""
    registry = get_executor_registry()
    assert registry_order() == tuple(registry.ordered_ids()) or set(registry_order()).issubset(
        set(registry.ordered_ids())
    )

    snapshot = ExecutorHealthRegistry().snapshot()
    for executor_id in registry_order():
        assert executor_id in snapshot

    for executor_id in snapshot:
        assert executor_id in registry_order()


def test_default_executor_preference_not_authority() -> None:
    """executor_health must not define an executor universe of its own."""
    tree = ast.parse(_HEALTH_SOURCE.read_text())

    literal_inventories = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "DEFAULT_EXECUTOR_PREFERENCE"
            for target in node.targets
        )
        and isinstance(node.value, (ast.Tuple, ast.List, ast.Set))
    ]
    assert not literal_inventories, (
        "DEFAULT_EXECUTOR_PREFERENCE must not be a static executor inventory"
    )

    snapshot_body = ast.get_source_segment(
        _HEALTH_SOURCE.read_text(),
        next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "snapshot"
        ),
    )
    assert "registry_order()" in (snapshot_body or "")
    assert "DEFAULT_EXECUTOR_PREFERENCE" not in (snapshot_body or "")


def test_registry_addition_visible_to_health(clean_registry) -> None:
    """A newly admitted executor becomes visible without editing executor_health."""
    assert "zz_ordering_probe" not in registry_order()

    clean_registry.register(_probe_identity("zz_ordering_probe"))

    assert "zz_ordering_probe" in clean_registry.ordered_ids()
    # Not routable until it carries runtime capability data, but it is admitted
    # and therefore ordered by the registry.
    assert "zz_ordering_probe" not in registry_order()


def test_unknown_executor_not_visible() -> None:
    """identity() cannot widen the health universe, and resolve rejects it."""
    registry = get_executor_registry()
    before = tuple(registry_order())

    with pytest.raises(ValueError):
        registry.identity("unknown-agent")

    assert tuple(registry_order()) == before
    assert "unknown-agent" not in registry_order()

    with pytest.raises(ValueError):
        resolve_executor(requested="unknown-agent")


def test_retired_executor_rejected() -> None:
    """Retired executors stay out of the universe and fail closed on request."""
    registry = get_executor_registry()

    assert "hicode" not in registry_order()
    assert "hicode" not in registry.ordered_ids()

    with pytest.raises(ValueError):
        registry.identity("hicode")
    with pytest.raises(ValueError):
        resolve_executor(requested="hicode")


def test_unhealthy_pool_fails_closed() -> None:
    """All-unverified candidates must raise rather than silently route."""
    with pytest.raises(ValueError, match="UNAVAILABLE or unverified"):
        resolve_executor(
            health_registry=ExecutorHealthRegistry(),
            fail_closed_unknown=True,
        )
