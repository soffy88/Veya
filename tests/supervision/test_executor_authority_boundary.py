"""Executor authority boundary — locks the post-hicode-retirement topology.

These assertions are *architecture* contracts, not environment checks: they must
hold on a machine with no provider credentials and on one with every provider
logged in. Anything that depends on the host's launchers or tokens belongs in a
test that injects its own executor universe, not here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from veya.remote.executor_registry import (
    ExecutorRegistry,
    ExecutorRuntimeIdentity,
    normalize_executor_id,
)
from veya.supervision import runner as supervision_runner
from veya.supervision.runner import _DEPRECATED_EXECUTOR_NAMES, _active_executors

_SUPERVISION_DIR = Path(supervision_runner.__file__).parent

#: Supervision must not hold an executor inventory. It reads admission from the
#: registry instead, so any of these coming back is a second authority.
_FORBIDDEN_LIST_SYMBOLS = (
    "_KNOWN_EXECUTORS",
    "_AVAILABLE_EXECUTORS",
    "_SUPPORTED_EXECUTORS",
    "_EXECUTOR_LIST",
    "_SUPERVISION_EXECUTOR_MAP",
    "_EXECUTOR_CACHE",
    "_WORKER_REGISTRY",
)

#: Executor names a provider can legitimately run under.
_PROVIDER_EXECUTORS = frozenset(
    {"pi", "codex", "antigravity", "opencode", "claude_code", "grok", "dsh"}
)

#: Modules that still name executors for *validation* purposes only:
#: ``orchestrated`` validates a submitted plan's worker, ``retask`` bounds a
#: re-dispatch, ``models`` labels an ExecutorKind. None of them decides which
#: executors exist. Recorded as a known remainder, not treated as done.
_VALIDATION_SCOPE_LITERALS = frozenset({"orchestrated.py", "retask.py", "models.py"})


def test_supervision_defines_no_executor_inventory() -> None:
    """No supervision module may define an executor/worker list."""
    offenders: list[str] = []
    for source in sorted(_SUPERVISION_DIR.glob("*.py")):
        text = source.read_text(encoding="utf-8")
        for symbol in _FORBIDDEN_LIST_SYMBOLS:
            if symbol in text:
                offenders.append(f"{source.name}: {symbol}")
    assert offenders == [], f"supervision regained an executor list: {offenders}"


def test_supervision_has_no_admission_level_executor_literals() -> None:
    """Supervision must not decide *admission* from its own literals.

    Remaining hardcoded names are validation-scope only (rejecting/relabelling a
    plan), not a source of admission: admission is the registry's answer.  They
    are enumerated here so the remainder is visible rather than assumed gone —
    see ``_VALIDATION_SCOPE_LITERALS``.
    """
    admission_offenders: list[str] = []
    validation_scope: dict[str, list[str]] = {}
    for source in sorted(_SUPERVISION_DIR.glob("*.py")):
        if source.name == "reap.py":
            continue  # stale-process identification, not executor knowledge
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in _PROVIDER_EXECUTORS
            ):
                owner = source.name
                entry = f"{source.name}:{node.lineno} {node.value!r}"
                if owner in _VALIDATION_SCOPE_LITERALS:
                    validation_scope.setdefault(owner, []).append(entry)
                else:
                    admission_offenders.append(entry)
    assert admission_offenders == [], (
        f"supervision decides admission from its own literals: {admission_offenders}"
    )
    # Recorded, not silently accepted: these are the known validation-scope
    # remainder and must not grow.
    assert set(validation_scope) <= set(_VALIDATION_SCOPE_LITERALS)


def test_admission_comes_from_the_registry_not_a_local_set() -> None:
    """The active set is exactly what the registry declares."""
    from veya.remote.executor_registry import get_executor_registry

    # Read the active set first: doing so declares the in-process substrate into
    # the registry (it has no provider contract to discover), so the snapshot is
    # only comparable afterwards.
    active = set(_active_executors())
    assert active == set(get_executor_registry().snapshot())
    # The registry remains the only place that knows builtin exists.
    assert "builtin" in active


def test_retired_names_are_denied_and_never_offered() -> None:
    """The deny set is deny-only: retired names are refused, not substituted."""
    assert frozenset({"hicode"}) == _DEPRECATED_EXECUTOR_NAMES
    assert not (_DEPRECATED_EXECUTOR_NAMES & set(_active_executors()))
    for name in _DEPRECATED_EXECUTOR_NAMES:
        assert (
            supervision_runner.executor_hint(
                type(
                    "_M",
                    (),
                    {"policies": type("_P", (), {"execution_policy": {"assignee_hint": name}})()},
                )()
            )
            is None
        )


def test_registry_fails_closed_on_retired_lookup() -> None:
    """The registry itself refuses a retired executor id."""
    with pytest.raises(ValueError, match="retired"):
        ExecutorRegistry().identity("hicode")


def test_registry_register_is_idempotent_and_snapshot_is_read_only() -> None:
    """Registering twice does not fork identity; querying never mutates."""
    registry = ExecutorRegistry()
    identity = ExecutorRuntimeIdentity(
        executor_id="probe",
        executor_kind="in_process_substrate",
        provider=None,
        model=None,
        auth_state="NOT_REQUIRED",
        reachable=True,
        launcher=None,
    )
    registry.register(identity)
    first = len(registry.snapshot())
    registry.register(identity)
    assert len(registry.snapshot()) == first
    before = dict(registry.snapshot())
    for _ in range(3):
        registry.snapshot()
    assert dict(registry.snapshot()) == before


def test_registry_exposes_no_removal_authority() -> None:
    """Nothing can un-register an executor, so admission cannot be revoked locally."""
    for method in ("unregister", "remove", "delete", "drop"):
        assert not hasattr(ExecutorRegistry, method)


def test_alias_resolution_is_canonical() -> None:
    assert normalize_executor_id("claude-code") == "claude_code"
    assert normalize_executor_id("  OpenCode ") == "opencode"
