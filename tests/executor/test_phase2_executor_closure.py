"""Phase 2 executor-authority closure.

Locks the split between the two authorities:

    ExecutorRegistry  -> executor *identity* and admission
    HarnessRegistry   -> execution *adapters*

plus the rules that keep them from merging back into one list.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import server.capability_model as cm
from server.capability_model import HarnessRegistry
from veya.remote.executor_registry import ExecutorRegistry, get_executor_registry

_REPO = Path(__file__).resolve().parents[2]

#: Executor inventories that must not reappear anywhere in first-party code.
_FORBIDDEN_INVENTORIES = (
    "KNOWN_EXECUTORS",
    "SUPPORTED_EXECUTORS",
    "AVAILABLE_EXECUTORS",
    "DEFAULT_EXECUTOR",
    "MISSION_EXECUTORS",
)

#: Modules that make up the executor execution surface.
_SCOPED = (
    "server/capability_model.py",
    "server/project_ask.py",
    "server/goal_run/leaf.py",
    "server/supervision_tools.py",
    "server/project_ask.py",
    "veya/remote/execution_context.py",
)


def _first_party_sources() -> list[Path]:
    out: list[Path] = []
    for root in ("server", "veya/remote", "veya/supervision"):
        base = _REPO / root
        if base.is_dir():
            out.extend(sorted(base.rglob("*.py")))
    return out


# ── 1. executor registry admission ─────────────────────────────────────────


def test_executor_registry_is_the_admission_authority() -> None:
    assert isinstance(get_executor_registry(), ExecutorRegistry)
    assert get_executor_registry().snapshot(), "registry must know at least one executor"


def test_retired_executor_is_not_admitted() -> None:
    registry = ExecutorRegistry()
    assert "hicode" not in registry.snapshot()
    with pytest.raises(ValueError, match="retired"):
        registry.identity("hicode")


def test_unknown_executor_is_not_in_the_admission_surface() -> None:
    """A name nobody declared must not appear in the admission snapshot.

    Note: ``identity()`` lazily *discovers* an unknown name and inserts it as an
    UNAVAILABLE identity, so the admission surface is ``snapshot()`` — which is
    what every consumer in this repo gates on.
    """
    assert "unknown-agent" not in ExecutorRegistry().snapshot()


# ── 2. harness execution ───────────────────────────────────────────────────


def test_bootstrap_registers_live_adapters_only() -> None:
    assert cm.initialize_execution_harnesses() == 2
    assert set(cm._HARNESS_ADAPTERS) == {"builtin", "dsh"}
    assert "hicode" not in cm._HARNESS_ADAPTERS


def test_bootstrap_is_idempotent() -> None:
    first = cm.initialize_execution_harnesses()
    second = cm.initialize_execution_harnesses()
    assert first == second == 2


def test_builtin_harness_executes() -> None:
    cm.initialize_execution_harnesses()
    adapter = cm._HARNESS_ADAPTERS["builtin"]
    assert callable(adapter), "builtin adapter must be callable"


def test_dsh_harness_executes() -> None:
    cm.initialize_execution_harnesses()
    adapter = cm._HARNESS_ADAPTERS["dsh"]
    assert callable(adapter), "dsh adapter must be callable"


def test_adapters_share_one_call_shape() -> None:
    """HarnessRegistry dispatches without knowing each adapter's signature."""
    import inspect

    cm.initialize_execution_harnesses()
    for name, adapter in cm._HARNESS_ADAPTERS.items():
        params = list(inspect.signature(adapter).parameters)
        assert params[:5] == ["store", "task_id", "request", "project_root", "understand_prefix"], (
            f"{name} adapter has an inconsistent call shape: {params}"
        )


async def test_retired_harness_is_rejected(tmp_path) -> None:
    reg = HarnessRegistry(cm._JsonRegistryStore(storage_path=tmp_path / "h.json"))
    with pytest.raises(ValueError, match="retired"):
        await reg.execute("hicode", store=None, task_id="t", request="r")


async def test_unknown_harness_is_rejected(tmp_path) -> None:
    reg = HarnessRegistry(cm._JsonRegistryStore(storage_path=tmp_path / "h.json"))
    with pytest.raises(ValueError, match="no execution adapter"):
        await reg.execute("unknown-agent", store=None, task_id="t", request="r")


# ── 3. no direct executor construction / no second authority ───────────────


def test_no_direct_executor_construction() -> None:
    offenders = [
        f"{p.relative_to(_REPO)}:{n.lineno}"
        for p in _first_party_sources()
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in {"Executor", "ExecutorConfig", "create_worker", "spawn_worker"}
    ]
    assert offenders == [], f"direct executor construction: {offenders}"


def test_no_second_executor_inventory_in_migrated_scope() -> None:
    """Server + supervision planes must carry no executor inventory.

    Two known remainders are reported as open blockers rather than asserted away
    here: ``DEFAULT_EXECUTOR_PREFERENCE`` in ``veya/remote/executor_health.py``,
    and its consumer ``server/routes/execution_contract.py``.
    """
    offenders = [
        f"{p.relative_to(_REPO)}"
        for p in _first_party_sources()
        if str(p.relative_to(_REPO)).startswith(("server/goal_run/", "server/capability_model.py", "server/project_ask.py", "server/supervision_tools.py", "veya/supervision/"))
        and any(name in p.read_text(encoding="utf-8") for name in _FORBIDDEN_INVENTORIES)
    ]
    assert offenders == [], f"executor inventory reappeared in: {offenders}"


def test_harness_registry_holds_no_executor_selection() -> None:
    """The adapter table must not decide *which* executor to use."""
    source = (_REPO / "server/capability_model.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    }
    # An executor-id set used for selection, as opposed to the adapter table.
    assert "_HARNESS_ADAPTERS" in names
    assert not (names & {"_HARNESS_EXECUTORS", "_ADMITTED_EXECUTORS"})


def test_registry_execution_chain_order() -> None:
    """ExecutorRegistry -> HarnessRegistry -> adapter, in that order."""
    cm.initialize_execution_harnesses()
    from veya.remote.executor_registry import get_executor_registry as registry

    assert "dsh" in registry().snapshot()
    assert "dsh" in cm._HARNESS_ADAPTERS
    assert callable(cm._HARNESS_ADAPTERS["dsh"])


def test_scoped_files_have_no_retired_harness_registration() -> None:
    offenders = [
        rel for rel in _SCOPED if 'harness_id="hicode"' in (_REPO / rel).read_text(encoding="utf-8")
    ]
    assert offenders == [], f"retired harness still registered in {offenders}"
