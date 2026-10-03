"""Explicit executor admission — grok is admitted, discovery is not.

The registry used to admit ``grok`` as a side effect: importing a consumer that
looked it up discovered it into ``_identities``. Admission is now explicit
(``_KNOWN``), ``identity()`` is a read-only lookup, and ``snapshot()`` is the
single admission surface.
"""

from __future__ import annotations

import pytest

from veya.remote.executor_registry import ExecutorRegistry, get_executor_registry

#: Runtime consumers expect this executor to work; it must therefore be admitted.
_REQUIRED_EXECUTORS = ("pi", "codex", "antigravity", "opencode", "claude_code", "dsh", "grok")


def test_required_executors_are_admitted() -> None:
    """Test 1: every supported executor exists in the canonical admission set."""
    snapshot = get_executor_registry().snapshot()
    missing = [name for name in _REQUIRED_EXECUTORS if name not in snapshot]
    assert missing == [], f"not admitted: {missing}"


def test_grok_is_admitted_before_any_lookup() -> None:
    """Acceptance: grok exists without anyone asking for it first."""
    assert "grok" in ExecutorRegistry().snapshot()


def test_identity_does_not_mutate_the_registry() -> None:
    """Test 2: a lookup is read-only, for known and unknown names alike."""
    registry = ExecutorRegistry()
    before = dict(registry.snapshot())
    for name in ("pi", "grok", "dsh"):
        registry.identity(name)
    assert dict(registry.snapshot()) == before


def test_identity_of_unknown_is_rejected_and_does_not_admit() -> None:
    """Test 3: an unknown executor raises and is not discovered into the registry."""
    registry = ExecutorRegistry()
    before = set(registry.snapshot())
    with pytest.raises(ValueError, match="unknown executor"):
        registry.identity("unknown-agent")
    assert set(registry.snapshot()) == before
    assert "unknown-agent" not in registry.snapshot()


def test_identity_of_retired_is_rejected() -> None:
    """Test 4: a retired executor raises and never becomes admissible."""
    registry = ExecutorRegistry()
    with pytest.raises(ValueError, match="retired"):
        registry.identity("hicode")
    assert "hicode" not in registry.snapshot()


def test_alias_normalisation_still_resolves() -> None:
    """Normalisation is unchanged: aliases resolve to the canonical id."""
    registry = ExecutorRegistry()
    assert registry.identity("claude-code").executor_id == "claude_code"
    assert registry.identity("CLAUDE_CODE").executor_id == "claude_code"


def test_register_remains_the_only_way_to_add() -> None:
    """An explicitly registered executor is retrievable, and only then."""
    from veya.remote.executor_registry import ExecutorRuntimeIdentity

    registry = ExecutorRegistry()
    identity = ExecutorRuntimeIdentity(
        executor_id="adopted-worker",
        executor_kind="l1_worker",
        provider=None,
        model=None,
        auth_state="UNKNOWN",
        reachable=False,
        launcher=None,
    )
    with pytest.raises(ValueError):
        registry.identity("adopted-worker")
    registry.register(identity)
    assert registry.identity("adopted-worker") is identity


def test_tool_adapter_projection_reflects_the_registry() -> None:
    """Test 5: the CLI projection admits grok and excludes the retired executor."""
    from veya.remote import tool_adapter

    snapshot = get_executor_registry().snapshot()
    assert "grok" in tool_adapter._CLI_WORKERS
    assert "hicode" not in tool_adapter._CLI_WORKERS
    assert set(tool_adapter._CLI_WORKERS) <= set(snapshot)


def test_supervision_projection_reflects_the_registry() -> None:
    """Test 6: supervision's validation sets are registry projections."""
    from veya.supervision.orchestrated import L1_WORKERS
    from veya.supervision.retask import _RETASK_WORKERS

    assert "grok" in L1_WORKERS
    assert "grok" in _RETASK_WORKERS
    assert "hicode" not in L1_WORKERS
    assert "hicode" not in _RETASK_WORKERS


def test_importing_tool_adapter_does_not_admit_anything() -> None:
    """Phase 3: the import is a pure read of the admission surface."""
    import subprocess
    import sys

    program = (
        "from veya import platform; platform.load('obase')\n"
        "from veya.remote.executor_registry import get_executor_registry\n"
        "import veya.remote\n"
        "before = set(get_executor_registry().snapshot())\n"
        "import veya.remote.tool_adapter\n"
        "after = set(get_executor_registry().snapshot())\n"
        "assert before == after, sorted(after - before)\n"
        "assert 'grok' in after\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr[-800:]


def test_snapshot_is_a_copy() -> None:
    """Mutating the returned mapping cannot reach the registry."""
    registry = ExecutorRegistry()
    snapshot = registry.snapshot()
    snapshot.pop("pi", None)
    assert "pi" in registry.snapshot()
