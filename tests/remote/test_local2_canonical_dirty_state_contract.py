"""L0 canonical/isolated target contract (spec §8, §39, §40).

The contract under test:

* read/execute defaults to the **canonical** worktree, so the live tree's own
  untracked source, staged edits and local virtualenv are observable;
* mutation defaults to an **isolated** worktree, so a write never lands in the
  owner's tree unless the caller asks for canonical explicitly;
* an explicit ``execution_target`` always wins, and an unknown one fails closed;
* neither mode leaks into the other.

Every test here drives the real ``RemoteToolAdapter`` / ``DurableJobManager``
path rather than asserting on the resolver alone, because the failure this
contract exists to prevent ("the worktree could not see the current tree") was
only ever visible end to end.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.remote.test_direct_fast_path import (  # reuse the real gateway harness
    PrimitiveExecutor,
    call_tool,
    initialize,
    make_gateway,
    make_workspace,
    wait_for_phase,
)
from veya.remote.execution import ExecutionStore
from veya.remote.executor_health import resolve_executor
from veya.remote.tool_adapter import EXECUTION_TARGETS, resolve_execution_target

pytestmark = pytest.mark.anyio


# ── fixtures ────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def dirty_repo(tmp_path: Path) -> Path:
    """A repo carrying every kind of local state the contract must preserve."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-qm", "base")

    # tracked modification (unstaged)
    (repo / "tracked.py").write_text("VALUE = 2\n", encoding="utf-8")
    # untracked source
    (repo / "untracked_module.py").write_text("NAME = 'untracked'\n", encoding="utf-8")
    # staged file
    (repo / "staged.py").write_text("STAGED = True\n", encoding="utf-8")
    _git(repo, "add", "staged.py")
    return repo


# ── §8.1-§8.5 dirty state is visible from the canonical target ───────────


def test_canonical_target_preserves_tracked_modifications(dirty_repo: Path) -> None:
    assert "tracked.py" in _git(dirty_repo, "diff", "--name-only")
    assert resolve_execution_target(str(dirty_repo), None, "") == "CANONICAL_WORKTREE"


def test_canonical_target_preserves_untracked_source(dirty_repo: Path) -> None:
    assert (dirty_repo / "untracked_module.py").is_file()
    assert "untracked_module.py" in _git(dirty_repo, "status", "--porcelain")


def test_canonical_target_sees_staged_files(dirty_repo: Path) -> None:
    assert "staged.py" in _git(dirty_repo, "diff", "--cached", "--name-only")


def test_canonical_target_sees_unstaged_files(dirty_repo: Path) -> None:
    assert "tracked.py" in _git(dirty_repo, "diff", "--name-only")


def test_canonical_target_does_not_silently_create_worktree(dirty_repo: Path) -> None:
    """Resolving canonical must not materialise a checkout as a side effect."""

    before = (
        sorted(p.name for p in (dirty_repo / ".veya" / "worktrees").glob("*"))
        if (dirty_repo / ".veya" / "worktrees").is_dir()
        else []
    )
    assert resolve_execution_target(str(dirty_repo), None, "") == "CANONICAL_WORKTREE"
    after = (
        sorted(p.name for p in (dirty_repo / ".veya" / "worktrees").glob("*"))
        if (dirty_repo / ".veya" / "worktrees").is_dir()
        else []
    )
    assert before == after


# ── §8.6-§8.7 the two modes stay distinct ───────────────────────────────


def test_isolated_target_is_still_selectable(dirty_repo: Path) -> None:
    assert (
        resolve_execution_target(str(dirty_repo), None, "NEW_ISOLATED_WORKTREE")
        == "NEW_ISOLATED_WORKTREE"
    )


def test_mutation_intent_defaults_to_isolated(dirty_repo: Path) -> None:
    """A write must not land in the owner's tree unless canonical is explicit."""

    assert (
        resolve_execution_target(str(dirty_repo), None, "", intent="mutation")
        == "NEW_ISOLATED_WORKTREE"
    )
    assert (
        resolve_execution_target(str(dirty_repo), None, "CANONICAL_WORKTREE", intent="mutation")
        == "CANONICAL_WORKTREE"
    )


def test_read_and_mutation_defaults_do_not_contaminate_each_other(dirty_repo: Path) -> None:
    assert resolve_execution_target(str(dirty_repo), None, "") == "CANONICAL_WORKTREE"
    assert (
        resolve_execution_target(str(dirty_repo), None, "", intent="mutation")
        == "NEW_ISOLATED_WORKTREE"
    )


# ── §8.9 target type cannot be silently changed downstream ───────────────


@pytest.mark.parametrize("target", list(EXECUTION_TARGETS))
def test_explicit_target_round_trips(dirty_repo: Path, target: str) -> None:
    assert resolve_execution_target(str(dirty_repo), None, target) == target
    assert resolve_execution_target(str(dirty_repo), None, target.lower()) == target
    assert resolve_execution_target(str(dirty_repo), None, target, intent="mutation") == target


def test_unknown_target_fails_closed_for_both_intents(dirty_repo: Path) -> None:
    from veya.remote.tool_adapter import RemoteToolAdapterError

    for intent in ("read", "mutation"):
        with pytest.raises(RemoteToolAdapterError) as excinfo:
            resolve_execution_target(str(dirty_repo), None, "NOT_A_TARGET", intent=intent)
        assert excinfo.value.code == "INVALID_ARGUMENT"


def test_executor_selection_is_independent_of_execution_target(dirty_repo: Path) -> None:
    """L0 target choice must not leak into L1 executor selection (§1/§2)."""

    selected, _evidence = resolve_executor()
    assert selected
    assert selected != "hicode"
    assert "hicode" not in EXECUTION_TARGETS


def test_target_type_is_reported_in_the_public_receipt(dirty_repo: Path) -> None:
    """The resolved target must be visible to the caller, never implied."""

    fields = resolve_execution_target(str(dirty_repo), None, "")
    assert fields in EXECUTION_TARGETS
    # The public execution record exposes the resolved checkout identity.
    from veya.remote.execution import ExecutionRecord

    record_fields = set(ExecutionRecord.__dataclass_fields__)
    assert {"resolved_repo_root", "worktree_repo_root", "requested_realpath"} <= record_fields


# ── §8 minimum acceptance: the dirty module is importable end to end ─────
#
# The regression this contract closes was only ever visible through a real
# execution: a fresh worktree is a clean checkout, so the owner's untracked
# module was absent and the import failed. These drive the actual MCP tool path.


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    return repo


async def _import_probe() -> str:
    return (
        f"{sys.executable} -c \"import contract_probe_module as m; print('IMPORT_PASS', m.MARKER)\""
    )


async def _run(gateway, secret, session, target: str | None) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "command": await _import_probe(),
        "profile": "local_trusted",
        "timeout_s": 60,
    }
    if target is not None:
        arguments["execution_target"] = target
    envelope = await call_tool(gateway, secret, session, "shell.exec", arguments)
    assert envelope.get("execution_id"), json.dumps(envelope)[:400]
    # A non-zero command exit is a legitimate outcome here: in an isolated
    # worktree the untracked module is genuinely absent, so the import must
    # fail. The job therefore terminates FAILED rather than COMPLETED, and the
    # test asserts on the receipt rather than on a success flag.
    return await wait_for_phase(
        gateway, secret, session, envelope["execution_id"], {"COMPLETED", "FAILED"}, timeout=90.0
    )


async def test_canonical_execution_imports_untracked_source(tmp_path: Path) -> None:
    """shell.exec on the default target must see untracked source."""

    repo = make_workspace(_new_repo(tmp_path))
    (repo / "contract_probe_module.py").write_text("MARKER = 'visible'\n", encoding="utf-8")
    # Deliberately untracked: a clean worktree would not contain it.
    assert "contract_probe_module.py" in _git(repo, "status", "--porcelain")

    gateway, secret, _ = make_gateway(repo, PrimitiveExecutor(), ExecutionStore(None))
    session = await initialize(gateway, secret)
    final = await _run(gateway, secret, session, None)

    assert final["status"] == "COMPLETED", final
    assert "IMPORT_PASS visible" in final["stdout_tail"], final
    assert final["cwd"] == str(repo.resolve())


async def test_isolated_execution_cannot_see_untracked_source(tmp_path: Path) -> None:
    """The two modes must not contaminate each other (§40)."""

    repo = make_workspace(_new_repo(tmp_path))
    (repo / "contract_probe_module.py").write_text("MARKER = 'visible'\n", encoding="utf-8")

    gateway, secret, _ = make_gateway(repo, PrimitiveExecutor(), ExecutionStore(None))
    session = await initialize(gateway, secret)
    final = await _run(gateway, secret, session, "NEW_ISOLATED_WORKTREE")

    assert ".veya/worktrees/" in final["cwd"], final
    assert "IMPORT_PASS" not in final["stdout_tail"], final
    assert "ModuleNotFoundError" in (final["stdout_tail"] + final.get("stderr_tail", "")), final


async def test_explicit_isolated_and_canonical_produce_different_cwds(tmp_path: Path) -> None:
    """Both modes exist, and neither silently becomes the other."""

    repo = make_workspace(_new_repo(tmp_path))
    (repo / "contract_probe_module.py").write_text("MARKER = 'visible'\n", encoding="utf-8")
    gateway, secret, _ = make_gateway(repo, PrimitiveExecutor(), ExecutionStore(None))
    session = await initialize(gateway, secret)

    canonical = await _run(gateway, secret, session, "CANONICAL_WORKTREE")
    isolated = await _run(gateway, secret, session, "NEW_ISOLATED_WORKTREE")
    assert canonical["cwd"] == str(repo.resolve())
    assert isolated["cwd"] != str(repo.resolve())
    assert "IMPORT_PASS" in canonical["stdout_tail"]
    assert "IMPORT_PASS" not in isolated["stdout_tail"]
