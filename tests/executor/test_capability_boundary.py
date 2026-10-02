"""Capability authority separation: admission (who) vs runtime ability (what).

``ExecutorRegistry`` owns executor identity and admission.
``WorkerRuntime`` owns runtime lifecycle capability (``supports_sleep``,
``supports_revive``, ``supports_context_rollover``, ``recovery_capability``).

The two must not be conflated. In particular an executor that is admitted but
has no capability record is ``NOT_READY`` — it must fail closed, never be
silently substituted with a different executor.
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
from veya.remote.executor_registry import get_executor_registry, is_retired_executor
from veya.remote.worker_runtime import WORKER_CAPABILITIES, capabilities_for

_HEALTH_SOURCE = Path(executor_health.__file__)


def test_admission_is_registry_owned() -> None:
    """Who exists is decided by ExecutorRegistry, never by capability data."""
    source = ast.get_source_segment(
        _HEALTH_SOURCE.read_text(),
        next(
            node
            for node in ast.walk(ast.parse(_HEALTH_SOURCE.read_text()))
            if isinstance(node, ast.FunctionDef) and node.name == "resolve_executor"
        ),
    )
    # The admission decision consults the registry.
    assert "registry.ordered_ids()" in (source or "")
    # Capability data is not consulted to decide admission.
    admission_gate = (source or "").split("Explicit pin authority")[0]
    assert 'not in WORKER_CAPABILITIES:\n        raise ValueError(f"Unknown executor' not in (
        admission_gate or ""
    )


def test_unknown_executor_rejected_as_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown executor"):
        resolve_executor(requested="unknown-agent")


def test_retired_executor_rejected() -> None:
    assert is_retired_executor("hicode") is True
    with pytest.raises(ValueError):
        resolve_executor(requested="hicode")


def test_admitted_without_capability_is_not_ready_not_substituted() -> None:
    """`acp` is admitted but has no capability record.

    It must fail closed. Silently resolving it to a different executor would be
    an unrequested substitution, which is exactly what this boundary forbids.
    """
    registry = get_executor_registry()
    assert "acp" in registry.ordered_ids(), "acp must be admitted for this test to be meaningful"
    assert "acp" not in WORKER_CAPABILITIES, "acp must lack capability data"

    with pytest.raises(ValueError, match="not ready"):
        resolve_executor(requested="acp")


def test_missing_capability_never_falls_back() -> None:
    """No substitution: the resolved executor must equal the requested one."""
    for requested in sorted(WORKER_CAPABILITIES):
        resolved, _ = resolve_executor(requested=requested, explicit_pin=True)
        assert resolved == requested


def test_runtime_capability_separation() -> None:
    """Capability lookup answers runtime ability and mutates nothing."""
    registry = get_executor_registry()
    before = dict(registry.snapshot())

    for executor_id in registry_order():
        caps = capabilities_for(executor_id)
        assert caps is not None

    assert dict(registry.snapshot()) == before


def test_capability_data_cannot_admit_an_executor() -> None:
    """Capability keys must stay a subset of registry admission."""
    admitted = set(get_executor_registry().snapshot())
    assert set(WORKER_CAPABILITIES).issubset(admitted)


def test_health_snapshot_excludes_admitted_but_unroutable() -> None:
    """The visible universe equals the routable set, not the admitted set."""
    registry = get_executor_registry()
    snapshot = ExecutorHealthRegistry().snapshot()

    assert set(snapshot) == set(registry_order())
    assert "acp" not in snapshot, "acp is admitted but not routable; it must not be visible"
    assert "acp" in registry.ordered_ids(), "registry still reports acp as admitted"


def test_fail_closed_when_nothing_qualifies() -> None:
    with pytest.raises(ValueError, match="UNAVAILABLE or unverified"):
        resolve_executor(
            health_registry=ExecutorHealthRegistry(),
            fail_closed_unknown=True,
        )
