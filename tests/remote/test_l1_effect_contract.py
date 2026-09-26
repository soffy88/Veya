from __future__ import annotations

import subprocess
from pathlib import Path

from veya.remote.execution_worktree import ExecutionWorktreeRegistry
from veya.remote.l1_contract import (
    EffectReceipt,
    L1ExecutionFinalizer,
    L1TaskContract,
    PromotionPolicy,
    WorkerResult,
)


def make_repo(path: Path) -> Path:
    path.mkdir()
    (path / "base.txt").write_text("BASE\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True)
    return path


def result(execution_id: str, worktree: str, exit_code: int = 0) -> WorkerResult:
    return WorkerResult(
        textual_summary="Done.",
        process_exit_code=exit_code,
        effect_receipt=EffectReceipt(
            execution_id=execution_id,
            worker_runtime_id="opencode-runtime",
            worker_type="OPENCODE",
            repo_identity="",
            worktree_path=worktree,
            task_kind="WRITE",
        ),
    )


def test_write_no_effect_is_not_success(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    registry = ExecutionWorktreeRegistry(tmp_path / "bindings")
    binding = registry.get_or_create("ex-noop", root)
    outcome = L1ExecutionFinalizer(registry).finalize(
        execution_id="ex-noop",
        repo=root,
        task_contract=L1TaskContract.write(verification_command="true"),
        worker_result=result("ex-noop", binding.worktree_path),
    )
    assert outcome.status == "FAILED"
    assert outcome.failure_class == "NO_EFFECT"


def test_wrong_effect_is_blocked(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    registry = ExecutionWorktreeRegistry()
    binding = registry.get_or_create("ex-wrong", root)
    (Path(binding.worktree_path) / "other.txt").write_text("wrong\n", encoding="utf-8")
    outcome = L1ExecutionFinalizer(registry).finalize(
        execution_id="ex-wrong",
        repo=root,
        task_contract=L1TaskContract.write(
            verification_command="true", allowed_files=["result.txt"]
        ),
        worker_result=result("ex-wrong", binding.worktree_path),
    )
    assert outcome.status == "FAILED"
    assert outcome.failure_class == "EFFECT_MISMATCH"


def test_write_verifies_commits_and_is_idempotent(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    registry = ExecutionWorktreeRegistry(tmp_path / "bindings")
    binding = registry.get_or_create("ex-write", root)
    (Path(binding.worktree_path) / "result.txt").write_text(
        "VEYA_OPENCODE_WRITE_OK\n", encoding="utf-8"
    )
    contract = L1TaskContract.write(
        promotion_policy=PromotionPolicy.MANUAL,
        verification_command='test "$(cat result.txt)" = VEYA_OPENCODE_WRITE_OK',
        allowed_files=["result.txt"],
    )
    finalizer = L1ExecutionFinalizer(registry)
    first = finalizer.finalize(
        execution_id="ex-write",
        repo=root,
        task_contract=contract,
        worker_result=result("ex-write", binding.worktree_path),
    )
    second = finalizer.finalize(
        execution_id="ex-write",
        repo=root,
        task_contract=contract,
        worker_result=result("ex-write", binding.worktree_path),
    )
    assert first.status == "PROMOTABLE"
    assert first.commit_sha
    assert second.status == "PROMOTABLE"
    assert second.commit_sha == first.commit_sha


def test_auto_promotion_updates_branch_only_after_finalization(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    registry = ExecutionWorktreeRegistry()
    binding = registry.get_or_create("ex-promote", root)
    (Path(binding.worktree_path) / "result.txt").write_text("ok\n", encoding="utf-8")
    outcome = L1ExecutionFinalizer(registry).finalize(
        execution_id="ex-promote",
        repo=root,
        task_contract=L1TaskContract.write(
            promotion_policy=PromotionPolicy.AUTO_AFTER_VERIFY,
            verification_command="test -f result.txt",
            allowed_files=["result.txt"],
        ),
        worker_result=result("ex-promote", binding.worktree_path),
    )
    assert outcome.status == "PROMOTED"
    assert (root / "result.txt").exists() is False
    promoted = subprocess.check_output(
        ["git", "-C", str(root), "show", f"{outcome.commit_sha}:result.txt"], text=True
    )
    assert promoted == "ok\n"
