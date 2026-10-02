"""Hicode retirement boundary: deny marker only, no runtime execution.

The retained ``hicode`` reference in ``ExecutorRegistry`` is a *retired deny
marker*: it names a known-removed executor so a stale request fails closed with
a clear error instead of being treated as merely unknown. It is not a runtime
execution path, and it must never be reachable for dispatch, probing, or
fallback.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from veya.remote.execution_contract import probe_runtime_capability_manifest
from veya.remote.executor_health import ExecutorHealthRegistry, registry_order, resolve_executor
from veya.remote.executor_registry import (
    ExecutorRegistry,
    get_executor_registry,
    is_retired_executor,
    normalize_executor_id,
)
from veya.remote.worker_runtime import WORKER_CAPABILITIES

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RETIREMENT_SOURCE = (_REPO_ROOT / "veya/executor_retirement.py").read_text()
_REGISTRY_SOURCE = (_REPO_ROOT / "veya/remote/executor_registry.py").read_text()


def test_hicode_is_a_retired_deny_marker() -> None:
    """The deny marker lives in the neutral retirement module, not the remote plane."""
    from veya.executor_retirement import RETIRED_EXECUTORS

    assert is_retired_executor("hicode") is True
    assert "hicode" in RETIRED_EXECUTORS
    assert '"hicode"' in _RETIREMENT_SOURCE


def test_retirement_marker_is_absent_from_the_remote_plane() -> None:
    """veya/remote must carry no retired-executor name of its own."""
    assert "hicode" not in _REGISTRY_SOURCE, (
        "the retired marker must live in veya/executor_retirement.py"
    )
    assert "_RETIRED_EXECUTORS" not in _REGISTRY_SOURCE


def test_hicode_admission_rejected() -> None:
    registry = get_executor_registry()
    with pytest.raises(ValueError, match="retired"):
        registry.identity("hicode")


def test_hicode_alias_also_rejected() -> None:
    """Normalization cannot launder a retired executor into acceptance."""
    registry = get_executor_registry()
    with pytest.raises(ValueError):
        registry.identity(normalize_executor_id("HICODE"))
    assert normalize_executor_id("HICODE") == "hicode"


def test_hicode_resolution_rejected() -> None:
    with pytest.raises(ValueError):
        resolve_executor(requested="hicode")


def test_hicode_probe_raises_rather_than_reporting_unavailable() -> None:
    """Retired != absent. Reporting UNAVAILABLE would hide a removed executor."""
    with pytest.raises(ValueError, match="retired"):
        probe_runtime_capability_manifest("hicode")


def test_hicode_never_appears_in_routing_or_health() -> None:
    registry = get_executor_registry()
    assert "hicode" not in registry_order()
    assert "hicode" not in registry.ordered_ids()
    assert "hicode" not in ExecutorHealthRegistry().snapshot()
    assert "hicode" not in WORKER_CAPABILITIES


def test_hicode_never_substituted_as_fallback() -> None:
    """No resolution path may yield hicode, including degraded fallback."""
    with pytest.raises(ValueError):
        resolve_executor(
            requested="hicode",
            health_registry=ExecutorHealthRegistry(),
            fail_closed_unknown=True,
        )
    resolved, _ = resolve_executor()
    assert resolved != "hicode"


def test_registry_rejects_retired_on_construction_path() -> None:
    """Registering a retired executor must not resurrect it."""
    registry = ExecutorRegistry()
    assert is_retired_executor("hicode")
    with pytest.raises(ValueError):
        registry.identity("hicode")


def test_no_hicode_dispatch_in_selection_code() -> None:
    """The health/selection plane contains no hicode dispatch branch."""
    health_source = (_REPO_ROOT / "veya/remote/executor_health.py").read_text()
    tree = ast.parse(health_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            rendered = ast.dump(node)
            assert "hicode" not in rendered, (
                f"executor_health contains a hicode comparison at line {node.lineno}"
            )
