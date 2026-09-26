from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path

import pytest

from runtime.execution.durable import DurableExecutionRepository
from runtime.execution.side_effects import SideEffectLedger
from veya.remote.execution_worktree import (
    CanonicalPromotionService,
    ExecutionWorktreeError,
    ExecutionWorktreeRegistry,
    PromotionConflict,
)
from veya.remote.workspace_binding import git_repo_identity


def repo(path: Path, name: str = "main") -> Path:
    path.mkdir()
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", name, str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True)
    return path


def commit(path: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", message], check=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def test_one_execution_one_repo_and_durable_reconnect(tmp_path: Path) -> None:
    main = repo(tmp_path / "veya")
    store = tmp_path / "bindings"
    first = ExecutionWorktreeRegistry(store)
    a = first.get_or_create("ex-1", main, goal_run_id="goal-1")
    b = first.get_or_create("ex-1", main)
    assert a.worktree_path == b.worktree_path
    assert a.base_sha == b.base_sha
    assert (Path(a.worktree_path) / "state.txt").exists() is False

    (Path(a.worktree_path) / "state.txt").write_text("kept\n", encoding="utf-8")
    second = ExecutionWorktreeRegistry(store)
    resumed = second.resolve("ex-1", main)
    assert Path(resumed.worktree_path, "state.txt").read_text(encoding="utf-8") == "kept\n"


def test_same_execution_different_repo_and_nested_identity(tmp_path: Path) -> None:
    parent = repo(tmp_path / "projects")
    child = repo(parent / "child")
    registry = ExecutionWorktreeRegistry(tmp_path / "bindings")
    left = registry.get_or_create("ex-1", parent)
    right = registry.get_or_create("ex-1", child)
    assert left.worktree_path != right.worktree_path
    assert left.repo_identity != right.repo_identity
    assert git_repo_identity(child) == right.repo_identity


def test_write_lease_single_writer_and_expiry_recovery(tmp_path: Path, monkeypatch) -> None:
    main = repo(tmp_path / "veya")
    registry = ExecutionWorktreeRegistry()
    identity = git_repo_identity(main)
    first = registry.acquire_lease("ex-1", identity, "worker-a", ttl_s=1)
    with pytest.raises(ExecutionWorktreeError, match="worker-a"):
        registry.acquire_lease("ex-1", identity, "worker-b", ttl_s=1)
    first.expires_at = 0
    assert registry.reap_leases() == 1
    registry.acquire_lease("ex-1", identity, "worker-b", ttl_s=1)


def test_promotion_fast_path_is_cas_and_idempotent(tmp_path: Path) -> None:
    main = repo(tmp_path / "veya")
    registry = ExecutionWorktreeRegistry(tmp_path / "bindings")
    binding = registry.get_or_create("ex-1", main)
    worktree = Path(binding.worktree_path)
    (worktree / "change.txt").write_text("change\n", encoding="utf-8")
    execution_commit = commit(worktree, "task change")
    binding.commit_sha = execution_commit
    registry.mark(binding, "PROMOTABLE")
    evidence = CanonicalPromotionService(registry).promote(
        "ex-1", main, expected_base_sha=binding.base_sha, verification_evidence={"tests": "pass"}
    )
    assert evidence.execution_commit_sha == execution_commit
    assert evidence.canonical_after_sha == execution_commit
    assert registry.resolve("ex-1", main).state == "PROMOTED"
    repeated = CanonicalPromotionService(registry).promote(
        "ex-1", main, expected_base_sha=binding.base_sha
    )
    assert repeated.promotion_mode == "IDEMPOTENT_ALREADY_PROMOTED"


def test_promotion_records_existing_side_effect_ledger(tmp_path: Path) -> None:
    class Ledger:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        async def execute(self, **kwargs):
            self.requests.append(kwargs)
            return kwargs["request"]

    main = repo(tmp_path / "veya")
    registry = ExecutionWorktreeRegistry(tmp_path / "bindings")
    binding = registry.get_or_create("ex-ledger", main)
    worktree = Path(binding.worktree_path)
    (worktree / "change.txt").write_text("change\n", encoding="utf-8")
    binding.commit_sha = commit(worktree, "task change")
    registry.mark(binding, "PROMOTABLE")
    ledger = Ledger()
    evidence = CanonicalPromotionService(registry, ledger).promote(
        "ex-ledger",
        main,
        expected_base_sha=binding.base_sha,
        verification_evidence={"tests": "pass"},
    )
    assert evidence.canonical_after_sha == binding.commit_sha
    assert len(ledger.requests) == 1
    assert ledger.requests[0]["operation_type"] == "CANONICAL_GIT_PROMOTION"
    assert ledger.requests[0]["request"]["idempotency_key"] == ledger.requests[0]["operation_key"]


def test_promotion_durable_ledger_reconnect_and_replay(tmp_path: Path) -> None:
    main = repo(tmp_path / "veya")
    registry_root = tmp_path / "bindings"
    ledger_path = tmp_path / "execution.sqlite3"
    repository = DurableExecutionRepository(sqlite_path=ledger_path)
    asyncio.run(repository.connect())
    try:
        registry = ExecutionWorktreeRegistry(registry_root)
        binding = registry.get_or_create("ex-ledger-durable", main, goal_run_id="goal-ledger")
        worktree = Path(binding.worktree_path)
        (worktree / "change.txt").write_text("change\n", encoding="utf-8")
        binding.commit_sha = commit(worktree, "task change")
        registry.mark(binding, "PROMOTABLE")
        first = CanonicalPromotionService(registry, SideEffectLedger(repository)).promote(
            "ex-ledger-durable",
            main,
            expected_base_sha=binding.base_sha,
            verification_evidence={"tests": "pass"},
        )
        assert first.canonical_after_sha == binding.commit_sha

        operation_key = hashlib.sha256(
            "\0".join(
                (
                    first.execution_id,
                    first.repo_identity,
                    first.execution_commit_sha,
                    first.target_branch,
                )
            ).encode()
        ).hexdigest()
        first_row = asyncio.run(repository.get_side_effect(operation_key))
        assert first_row is not None
        assert first_row["state"] == "committed"
        assert first_row["operation_type"] == "CANONICAL_GIT_PROMOTION"
        assert first_row["goal_run_id"] == "goal-ledger"
        first_revision = first_row["revision"]
    finally:
        asyncio.run(repository.close())

    # Recreate both durable readers before replaying the same promotion.
    restored_registry = ExecutionWorktreeRegistry(registry_root)
    restored_binding = restored_registry.resolve("ex-ledger-durable", main)
    assert restored_binding.state == "PROMOTED"
    assert restored_binding.commit_sha == first.execution_commit_sha
    replay_repository = DurableExecutionRepository(sqlite_path=ledger_path)
    asyncio.run(replay_repository.connect())
    try:
        replay_row = asyncio.run(replay_repository.get_side_effect(operation_key))
        assert replay_row is not None
        assert replay_row["state"] == "committed"
        replay = CanonicalPromotionService(
            restored_registry, SideEffectLedger(replay_repository)
        ).promote(
            "ex-ledger-durable",
            main,
            expected_base_sha=restored_binding.base_sha,
        )
        assert replay.promotion_mode == "IDEMPOTENT_ALREADY_PROMOTED"
        assert (
            subprocess.check_output(
                ["git", "-C", str(main), "rev-parse", f"refs/heads/{first.target_branch}"],
                text=True,
            ).strip()
            == first.execution_commit_sha
        )
        replay_row_after = asyncio.run(replay_repository.get_side_effect(operation_key))
        assert replay_row_after is not None
        assert replay_row_after["state"] == "committed"
        assert replay_row_after["revision"] == first_revision
    finally:
        asyncio.run(replay_repository.close())


def test_promotion_blocks_canonical_divergence(tmp_path: Path) -> None:
    main = repo(tmp_path / "veya")
    registry = ExecutionWorktreeRegistry()
    binding = registry.get_or_create("ex-1", main)
    worktree = Path(binding.worktree_path)
    (worktree / "change.txt").write_text("task\n", encoding="utf-8")
    binding.commit_sha = commit(worktree, "task")
    registry.mark(binding, "PROMOTABLE")
    (main / "other.txt").write_text("canonical\n", encoding="utf-8")
    commit(main, "canonical advanced")
    with pytest.raises(PromotionConflict, match="canonical advanced"):
        CanonicalPromotionService(registry).promote(
            "ex-1",
            main,
            expected_base_sha=binding.base_sha,
            verification_evidence={"tests": "pass"},
        )
