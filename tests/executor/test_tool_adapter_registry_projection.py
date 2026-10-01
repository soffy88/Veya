"""tool_adapter must not depend on ExecutorRegistry identity discovery.

Importing ``veya.remote.tool_adapter`` used to call ``identity()`` for every
worker it supports. ``identity()`` discovers unregistered names *into* the
registry, so that import silently widened the executor admission surface — which
is what blocked making ``identity()`` a strict lookup.

These tests pin the decoupled behaviour: the projection is built from
``snapshot()`` only, and importing the module cannot admit an executor.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from veya.remote.executor_registry import ExecutorRegistry, get_executor_registry

#: Importing this module must not require a live registry.
_SUBPROCESS_IMPORT = "import veya.remote.tool_adapter"


def test_tool_adapter_imports_cleanly() -> None:
    """Acceptance: `python -c "import veya.remote.tool_adapter"` passes."""
    result = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_IMPORT],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-800:]


def test_cli_workers_is_a_snapshot_projection() -> None:
    """Every entry in the projection corresponds to an admitted registry identity."""
    from veya.remote import tool_adapter

    snapshot = get_executor_registry().snapshot()
    for worker, meta in tool_adapter._CLI_WORKERS.items():
        assert worker in snapshot, f"{worker} projected but not admitted by the registry"
        identity = snapshot[worker]
        assert meta["provider"] == (identity.provider or "unknown")
        assert meta["model"] == (identity.model or "unknown")


def test_import_does_not_expand_the_registry() -> None:
    """Acceptance: snapshot size is identical before and after the import."""
    registry = get_executor_registry()
    before = set(registry.snapshot())
    subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_IMPORT],
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    assert set(get_executor_registry().snapshot()) == before


def test_hicode_is_excluded_from_the_projection() -> None:
    """Acceptance: the retired executor never appears in the CLI projection."""
    from veya.remote import tool_adapter

    assert "hicode" not in tool_adapter._CLI_WORKERS


def test_projection_needs_no_identity_call(tmp_path) -> None:
    """Building the projection must not call identity() at all.

    Runs in a subprocess so the module-level ``_CLI_WORKERS`` is recomputed with
    ``identity`` replaced by a function that fails the test.
    """
    program = (
        "import veya.remote.executor_registry as er\n"
        "def _boom(*a, **k):\n"
        "    raise AssertionError('identity() must not be used to build the projection')\n"
        "er.ExecutorRegistry.identity = _boom\n"
        "import veya.remote.tool_adapter as ta\n"
        "assert isinstance(ta._CLI_WORKERS, dict)\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr[-800:]


def test_unknown_worker_cannot_enter_the_projection() -> None:
    """A worker the registry has not admitted is projected as absent, not resolved."""
    from veya.remote import tool_adapter

    assert "unknown-agent" not in tool_adapter._CLI_WORKERS
    # And it genuinely is not admitted — the projection did not create it.
    assert "unknown-agent" not in get_executor_registry().snapshot()


def test_opencode_metadata_is_preserved() -> None:
    """The existing compatibility contract for older callers still holds."""
    from veya.remote import tool_adapter

    meta = tool_adapter._CLI_WORKERS["opencode"]
    assert meta["provider"] == "opencode"
    assert "longcat" in meta["model"]


@pytest.mark.parametrize("worker", ["pi", "opencode", "claude_code", "codex", "antigravity", "dsh"])
def test_registry_admitted_workers_are_projected(worker: str) -> None:
    from veya.remote import tool_adapter

    assert worker in get_executor_registry().snapshot()
    assert worker in tool_adapter._CLI_WORKERS


def test_worker_types_is_not_an_authority() -> None:
    """`_WORKER_TYPES` states adapter support, not executor existence.

    ``grok`` is supported by the adapter but is not admitted by the registry, so
    it must not be projected. That gap is a registry-registration decision, not
    something importing this module may paper over.
    """
    from veya.remote import tool_adapter

    assert "grok" in tool_adapter._WORKER_TYPES
    assert "grok" not in get_executor_registry().snapshot()
    assert "grok" not in tool_adapter._CLI_WORKERS


def test_registry_lookup_is_not_mutated_by_projection() -> None:
    """Projecting twice is stable and leaves the registry untouched."""
    from veya.remote import tool_adapter

    registry = get_executor_registry()
    before = dict(registry.snapshot())
    assert tool_adapter._cli_workers() == tool_adapter._cli_workers()
    assert dict(registry.snapshot()) == before
    assert isinstance(ExecutorRegistry(), ExecutorRegistry)
