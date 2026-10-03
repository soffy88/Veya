"""Final executor authority boundary.

One authority per concern:
  - ``ExecutorRegistry`` owns executor identity, admission, ordering, aliases.
  - ``HarnessRegistry`` owns harness registration/execution.
  - ``WorkerRuntime`` owns runtime lifecycle capability.

These tests fail if a second registry, a second inventory, a discovery path, or
a retired-executor fallback reappears.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from veya.remote import executor_health
from veya.remote.executor_health import (
    ExecutorHealthRegistry,
    normalize_executor_name,
    registry_order,
    resolve_executor,
)
from veya.remote.executor_registry import (
    ExecutorRegistry,
    get_executor_registry,
    is_retired_executor,
    normalize_executor_id,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _production_sources() -> list[Path]:
    skip = {"tests", "platform", ".venv", "venv", "node_modules", "build", "dist"}
    files: list[Path] = []
    for top in ("veya", "server", "runtime"):
        root = _REPO_ROOT / top
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if any(part in skip for part in path.parts):
                continue
            files.append(path)
    return files


def test_no_direct_executor_construction_in_production() -> None:
    """ExecutorRegistry is a singleton reached through get_executor_registry().

    The only permitted construction is inside the factory itself, in
    ``veya/remote/executor_registry.py``.
    """
    owner = (_REPO_ROOT / "veya/remote/executor_registry.py").resolve()
    offenders: list[str] = []
    for path in _production_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "ExecutorRegistry":
                if path.resolve() == owner:
                    # Permitted: the singleton factory's own construction.
                    continue
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, f"direct ExecutorRegistry construction outside the factory: {offenders}"


def test_executor_registry_is_single_singleton() -> None:
    """get_executor_registry() always returns the same instance."""
    assert get_executor_registry() is get_executor_registry()


def test_unknown_executor_discovery_rejected() -> None:
    """identity() is a strict lookup and never admits."""
    registry = get_executor_registry()
    before = dict(registry.snapshot())

    with pytest.raises(ValueError):
        registry.identity("unknown-agent")
    with pytest.raises(ValueError):
        resolve_executor(requested="unknown-agent")

    assert dict(registry.snapshot()) == before


def test_retired_executor_rejected_and_never_falls_back() -> None:
    """Retired executors fail closed everywhere and cannot re-enter."""
    registry = get_executor_registry()
    before = dict(registry.snapshot())

    assert is_retired_executor("hicode") is True

    with pytest.raises(ValueError):
        registry.identity("hicode")
    with pytest.raises(ValueError):
        resolve_executor(requested="hicode")

    assert "hicode" not in registry_order()
    assert "hicode" not in registry.ordered_ids()
    assert dict(registry.snapshot()) == before


def test_registry_lookup_does_not_mutate() -> None:
    """Lookup and health observation are side-effect free."""
    registry = get_executor_registry()
    before = dict(registry.snapshot())

    for executor_id in registry.ordered_ids():
        registry.identity(executor_id)
    ExecutorHealthRegistry().snapshot()

    assert dict(registry.snapshot()) == before


def test_no_second_executor_inventory_in_executor_health() -> None:
    """executor_health owns no executor list, ordering list, or alias table."""
    source = (Path(executor_health.__file__)).read_text()
    tree = ast.parse(source)

    banned_assignments = {
        "DEFAULT_EXECUTOR_PREFERENCE",
        "EXECUTOR_ALIASES",
        "KNOWN_EXECUTORS",
        "VALID_EXECUTORS",
        "MISSION_EXECUTORS",
        "EXECUTOR_ORDER",
        "EXECUTOR_PRIORITY_TABLE",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in banned_assignments:
                    pytest.fail(f"executor_health defines a second authority: {target.id}")

    # No hardcoded per-executor dispatch inside the health plane.
    assert "if executor ==" not in source
    assert "if worker ==" not in source


def test_alias_authority_single_source() -> None:
    """All alias forms resolve through the registry, in one direction only."""
    for raw, expected in (
        ("agy", "antigravity"),
        ("AGY", "antigravity"),
        ("claude-code", "claude_code"),
        ("opencode-go", "opencode-go"),
    ):
        assert normalize_executor_name(raw) == normalize_executor_id(raw)
        assert normalize_executor_name(raw) == expected


def test_registry_order_is_deterministic() -> None:
    """Repeated reads return the same canonical order."""
    registry = get_executor_registry()
    assert registry.ordered_ids() == registry.ordered_ids()
    assert registry_order() == registry_order()


def test_health_snapshot_contains_exactly_admitted_routable_executors() -> None:
    """Health never invents or drops executors relative to the projection."""
    snapshot = ExecutorHealthRegistry().snapshot()
    assert set(snapshot) == set(registry_order())


def test_fail_closed_when_no_candidate_qualifies() -> None:
    """Unverified candidates must raise instead of silently routing."""
    with pytest.raises(ValueError, match="UNAVAILABLE or unverified"):
        resolve_executor(
            health_registry=ExecutorHealthRegistry(),
            fail_closed_unknown=True,
        )


def test_harness_registry_is_singleton_only() -> None:
    """HarnessRegistry is owned by capability_model, not duplicated elsewhere."""
    definitions: list[str] = []
    for path in _production_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "HarnessRegistry":
                definitions.append(str(path.relative_to(_REPO_ROOT)))
    assert definitions == ["server/capability_model.py"], (
        f"HarnessRegistry must be defined exactly once, found: {definitions}"
    )


def test_executor_registry_definition_is_single() -> None:
    """Only one ExecutorRegistry class definition exists in production code."""
    definitions: list[str] = []
    for path in _production_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ExecutorRegistry":
                definitions.append(str(path.relative_to(_REPO_ROOT)))
    assert definitions == ["veya/remote/executor_registry.py"], (
        f"ExecutorRegistry must be defined exactly once, found: {definitions}"
    )


def test_registry_class_is_not_reimported_under_alias() -> None:
    """A second executor inventory cannot hide behind an aliased import."""
    assert ExecutorRegistry.__module__ == "veya.remote.executor_registry"
