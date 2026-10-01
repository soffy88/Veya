"""End-to-end executor retirement closure — supervision plane.

Covers the contract the GoalRun/boss entrypoints *cannot* yet satisfy: the
supervision plane admits, validates and re-dispatches only through
ExecutorRegistry, with no retired name and no fallback anywhere.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from veya.remote.executor_registry import ExecutorRegistry, get_executor_registry
from veya.supervision import orchestrated as orchestrated_mod
from veya.supervision import retask as retask_mod
from veya.supervision import runner as supervision_runner
from veya.supervision.orchestrated import OrchestrationError, parse_subtasks, validate_plan
from veya.supervision.runner import _active_executors, _active_l1_executors

_SUPERVISION_DIR = Path(supervision_runner.__file__).parent

#: Executor names retired from the canonical registry.
_RETIRED_NAMES = frozenset({"hicode"})


def test_supervision_has_no_retired_executor_default() -> None:
    """The only places supervision may name a retired executor are deny/stale.

    Exactly two sanctioned sites exist: the deny set that *refuses* the name, and
    the reaper's stale-process marker. Any third occurrence — especially a
    keyword default — would mean a retired executor could be injected again.

    Scoped to supervision deliberately: the GoalRun/boss defaults live in
    ``server/goal_run/*`` and ``server/boss_entrypoint.py``, outside this module's
    contract and still hardcoding the name. Asserting they are clean would be
    false today; the gap is reported in the closure report instead.
    """
    sanctioned = {
        ("runner.py", "_DEPRECATED_EXECUTOR_NAMES"),
        ("reap.py", "reasonix"),
    }
    found: set[tuple[str, str]] = set()
    for source in sorted(_SUPERVISION_DIR.glob("*.py")):
        for line in source.read_text(encoding="utf-8").splitlines():
            if not any(f'"{name}"' in line for name in _RETIRED_NAMES):
                continue
            for file_name, marker in sanctioned:
                if source.name == file_name and marker in line:
                    found.add((file_name, marker))
                    break
            else:
                pytest.fail(
                    f"{source.name} names a retired executor outside a deny/stale site: "
                    f"{line.strip()!r}"
                )
    assert found == sanctioned, f"expected both sanctioned sites, saw {sorted(found)}"


def test_registry_is_only_executor_inventory() -> None:
    """Validation and re-dispatch sets are registry projections, not literals."""
    registry = get_executor_registry()
    assert set(_active_executors()) == set(registry.snapshot())
    expected_l1 = {n for n in registry.snapshot() if n != supervision_runner._LOCAL_EXECUTOR}
    assert set(orchestrated_mod._l1_workers()) == expected_l1
    assert set(retask_mod._retask_workers()) == expected_l1


def test_validation_lists_are_derived_not_declared() -> None:
    """L1_WORKERS / _RETASK_WORKERS carry no hand-written executor literal."""
    for module in (orchestrated_mod, retask_mod):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Tuple, ast.Set)):
                for element in ast.walk(node):
                    if (
                        isinstance(element, ast.Constant)
                        and element.value in _active_l1_executors()
                    ):
                        pytest.fail(
                            f"{module.__name__} declares the executor literal "
                            f"{element.value!r} instead of projecting the registry"
                        )


@pytest.mark.parametrize("worker", ["hicode"])
def test_retired_executor_fail_closed(worker: str) -> None:
    """A retired executor is rejected outright — never substituted."""
    with pytest.raises(OrchestrationError):
        validate_plan(parse_subtasks([{"task_id": "a", "objective": "A", "worker": worker}]))
    assert worker not in _active_executors()
    assert worker not in _active_l1_executors()


@pytest.mark.parametrize("worker", ["unknown-agent", "unknown", "nope"])
def test_unknown_executor_fail_closed(worker: str) -> None:
    """An unknown executor is rejected, not guessed."""
    with pytest.raises(OrchestrationError):
        validate_plan(parse_subtasks([{"task_id": "a", "objective": "A", "worker": worker}]))


def test_no_executor_authority_symbol_introduced() -> None:
    """No module may re-declare an executor authority list."""
    forbidden = ("KNOWN_EXECUTORS", "AVAILABLE_EXECUTORS", "SUPPORTED_EXECUTORS", "LOCAL_EXECUTORS")
    offenders = [
        f"{source.name}:{symbol}"
        for source in sorted(_SUPERVISION_DIR.glob("*.py"))
        for symbol in forbidden
        if symbol in source.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_registry_remains_the_only_identity_authority() -> None:
    """Sanity: the registry still refuses retired ids and holds no removal API."""
    with pytest.raises(ValueError, match="retired"):
        ExecutorRegistry().identity("hicode")
    assert not any(hasattr(ExecutorRegistry, m) for m in ("unregister", "remove", "delete"))
