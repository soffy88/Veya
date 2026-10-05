"""P0-N: a governed ``git.verify``.

``git.verify`` is not a second opinion about a commit, it is the governed way to
ask whether a SHA a caller was handed really is in this repository with the
properties claimed. It delegates to WorktreeManager like ``git.stage`` and
``git.commit`` do; it is not a new Git authority and it does not touch
``git.promote``.

Every result is cross-checked against the git CLI used purely as an independent
oracle — the CLI verifies this code, it does not stand in for it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from runtime.coding.worktree import WorktreeError, WorktreeManager
from veya.remote import (
    RemoteAudit,
    RemoteAuth,
    RemotePermissions,
    RemoteSessionManager,
    RemoteToolAdapter,
)
from veya.remote.execution import ExecutionStore
from veya.remote.mcp_server import create_gateway

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)


def git(path: Path, *args: str) -> str:
    """Independent oracle. Never the implementation under test."""
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout


def seed(base: Path, name: str = "proj") -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("# p\n", encoding="utf-8")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    for c in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        git(repo, *c)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    return repo


@pytest.fixture()
def committed(tmp_path: Path) -> tuple[Path, WorktreeManager, Path, str]:
    """A repo with one real commit made inside its isolated worktree."""
    repo = seed(tmp_path)
    manager = WorktreeManager(repo)
    worktree = Path(manager.create("task_p0n", "p0n").path)
    (worktree / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    manager.stage(worktree, paths=["calc.py"])
    sha = manager.commit(worktree, "fix add", expect_paths=["calc.py"])["commit_sha"]
    return repo, manager, worktree, sha


# ── the surface is narrow and delegates ───────────────────────────────
def test_git_verify_is_bound_to_its_own_canonical_tool() -> None:
    from veya.remote.tool_adapter import BINDING_INDEX

    binding = BINDING_INDEX["git.verify"]
    assert binding.veya_tool == "coding_git_verify"
    # It is a read: verification observes, it does not mutate.
    assert str(binding.effect).endswith("READ"), binding.effect
    # And a SHA is what makes it a verification rather than a diff.
    assert binding.schema["required"] == ["commit_sha"]


def test_git_verify_did_not_disturb_promote_or_the_other_git_tools() -> None:
    from veya.remote.tool_adapter import BINDING_INDEX, BINDINGS

    assert BINDING_INDEX["git.promote"].veya_tool == "git_promote"
    assert BINDING_INDEX["git.stage"].veya_tool == "coding_git_stage"
    assert BINDING_INDEX["git.commit"].veya_tool == "coding_git_commit"
    names = [b.name for b in BINDINGS]
    assert names.count("git.verify") == 1
    assert len(set(names)) == len(names), "duplicate binding names"
    root = Path(__file__).resolve().parents[2]
    text = (root / "veya" / "remote" / "git_promotion.py").read_text(encoding="utf-8")
    assert "Promotion != Commit" in text and "NO auto-commit, NO auto-push" in text


# ── happy path, every field checked against the oracle ────────────────
def test_verify_reports_what_git_holds(committed) -> None:
    _repo, manager, worktree, sha = committed

    result = manager.verify(worktree, sha, expect_paths=["calc.py"])

    assert result["verified"] is True
    assert result["commit_sha"] == git(worktree, "rev-parse", "HEAD").strip()
    assert result["parent_sha"] == git(worktree, "rev-parse", "HEAD^").strip()
    assert result["tree"] == git(worktree, "rev-parse", "HEAD^{tree}").strip()
    assert result["changed_paths"] == ["calc.py"]
    assert result["expected_paths"] == ["calc.py"]
    assert Path(result["path"]) == worktree


def test_verify_reports_the_repository_the_commit_lives_in(committed) -> None:
    _repo, manager, worktree, sha = committed

    result = manager.verify(worktree, sha)

    assert Path(result["repo_root"]).resolve() == Path(_repo).resolve()
    assert Path(result["path"]).resolve() == worktree.resolve()


# ── it fails closed ──────────────────────────────────────────────────
def test_a_sha_that_does_not_exist_is_refused(committed) -> None:
    _repo, manager, worktree, _sha = committed

    with pytest.raises(WorktreeError, match="not found"):
        manager.verify(worktree, "0" * 40)


def test_a_short_or_empty_sha_is_refused(committed) -> None:
    _repo, manager, worktree, _sha = committed

    for bad in ("", "   ", "abc"):
        with pytest.raises(WorktreeError, match="commit_sha"):
            manager.verify(worktree, bad)


def test_a_commit_from_another_repository_is_refused(tmp_path: Path) -> None:
    """The same SHA in a different repo must not verify here."""
    repo_a = seed(tmp_path / "a")
    repo_b = seed(tmp_path / "b")
    manager_a = WorktreeManager(repo_a)
    wt_a = Path(manager_a.create("task_a", "a").path)
    (wt_a / "only-in-a.txt").write_text("a\n", encoding="utf-8")
    manager_a.stage(wt_a, paths=["only-in-a.txt"])
    sha_a = manager_a.commit(wt_a, "commit in a")["commit_sha"]

    manager_b = WorktreeManager(repo_b)
    wt_b = Path(manager_b.create("task_b", "b").path)

    # It resolves in its own repository...
    assert manager_a.verify(wt_a, sha_a)["commit_sha"] == sha_a
    # ...and is not silently accepted in another one.
    with pytest.raises(WorktreeError):
        manager_b.verify(wt_b, sha_a)


def test_a_ref_expression_is_not_accepted_in_place_of_a_sha(committed) -> None:
    """``git.verify`` verifies the SHA it was handed, not a moving target.

    Rev-parse would happily resolve ``HEAD`` or ``sha~1``, and the result looks
    like a legitimate commit. A caller who passed a ref could get a "verified"
    answer about a commit they never named, so a ref is refused even though it
    resolves.

    The fixture is deepened first so the ref resolves to a commit that *has* a
    parent. Otherwise the only thing refusing it would be an unrelated
    "no such parent" failure, and the test would pass for the wrong reason.
    """
    _repo, manager, worktree, _sha = committed
    (worktree / "extra.txt").write_text("e\n", encoding="utf-8")
    manager.stage(worktree, paths=["extra.txt"])
    tip = manager.commit(worktree, "second", expect_paths=["extra.txt"])["commit_sha"]

    ref = f"{tip}~1"
    resolved = git(worktree, "rev-parse", f"{ref}^{{commit}}").strip()
    # It resolves to a real commit that has a real parent, and to a *different*
    # commit than the caller named — so nothing but the identity guard can
    # refuse it.
    assert resolved != tip
    assert git(worktree, "rev-parse", f"{resolved}^").strip()

    # The positive half: the real SHA does verify, so the negative is not vacuous.
    assert manager.verify(worktree, tip)["verified"] is True

    result = manager.verify(worktree, ref)
    assert result["verified"] is False
    assert result["ref_expression"] == ref
    assert result["resolved_sha"] == resolved


def test_an_unexpected_changed_path_set_is_refused(committed) -> None:
    _repo, manager, worktree, sha = committed

    with pytest.raises(WorktreeError, match="do not match"):
        manager.verify(worktree, sha, expect_paths=["README.md"])

    with pytest.raises(WorktreeError, match="do not match"):
        manager.verify(worktree, sha, expect_paths=["calc.py", "README.md"])


def test_the_canonical_tree_is_not_a_verifiable_target(committed) -> None:
    repo, manager, _worktree, sha = committed

    with pytest.raises(WorktreeError):
        manager.verify(repo, sha)


def test_an_unregistered_directory_is_refused(tmp_path: Path) -> None:
    repo = seed(tmp_path)
    WorktreeManager(repo).create("task_reg", "reg")
    outsider = repo / ".veya" / "worktrees" / "not-registered"
    outsider.mkdir()

    with pytest.raises(WorktreeError):
        WorktreeManager(repo).verify(outsider, "0" * 40)


# ── the canonical tool reports failure rather than raising ────────────
def test_the_canonical_tool_returns_failed_status(committed) -> None:
    from runtime.coding.tools import coding_git_verify

    repo, _manager, _worktree, _sha = committed

    bad = coding_git_verify(str(repo), "0" * 40)
    assert bad["status"] == "failed"
    assert "git verify failed" in json.dumps(bad, default=str)


# ── gateway level, which is where P0-M's lesson applies ───────────────
class Executor:
    def __init__(self) -> None:
        self.names: list[str] = []

    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        self.names.append(name)
        if name == "coding_worktree_create":
            manager = WorktreeManager(kwargs["workspace_path"])
            record = manager.create(str(kwargs["task_id"]), str(kwargs.get("objective") or "p0n"))
            return json.dumps({"status": "ok", "data": {"worktree": record.to_dict()}}, default=str)
        if name == "write_file":
            from server.tool_registry import _tool_write_file

            return json.dumps(
                {
                    "status": "ok",
                    "data": {
                        "result": str(
                            _tool_write_file(
                                str(kwargs.get("filepath")), str(kwargs.get("content", "")), True
                            )
                        )
                    },
                }
            )
        return json.dumps({"status": "ok", "data": {"stdout": "unused"}})


async def _gateway(repo: Path):
    auth = RemoteAuth()
    _record, secret = auth.issue("t", permissions=PERMS, workspaces=[str(repo)])
    audit = RemoteAudit(None)
    adapter = RemoteToolAdapter(
        Executor(), redact=audit.redact, execution_store=ExecutionStore(None)
    )
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=120, max_sessions=8),
        audit=audit,
        adapter=adapter,
    )
    session = (
        await gateway.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"workspace": str(repo)}},
            authorization=f"Bearer {secret}",
        )
    )["result"]["sessionId"]
    return gateway, secret, session


async def _call(gateway, secret, session, name, arguments):
    response = await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        authorization=f"Bearer {secret}",
        session_header=session,
    )
    return response["result"]["structuredContent"]


async def _lifecycle(gateway, secret, session, repo: Path) -> dict[str, Any]:
    """write -> stage -> commit -> verify, all through the gateway."""
    isolated = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}
    write = await _call(
        gateway,
        secret,
        session,
        "file.write",
        {**isolated, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
    )
    assert write["ok"] is True, write
    staged = await _call(gateway, secret, session, "git.stage", {**isolated, "paths": ["calc.py"]})
    assert staged["ok"] is True, staged
    committed = await _call(
        gateway,
        secret,
        session,
        "git.commit",
        {**isolated, "message": "fix add", "expect_paths": ["calc.py"]},
    )
    assert committed["ok"] is True, committed
    verified = await _call(
        gateway,
        secret,
        session,
        "git.verify",
        {**isolated, "commit_sha": committed["result"]["commit_sha"], "expect_paths": ["calc.py"]},
    )
    return {"staged": staged, "committed": committed, "verified": verified, "write": write}


async def test_verify_through_the_gateway_after_a_real_commit(tmp_path: Path) -> None:
    repo = seed(tmp_path)
    canonical_head = git(repo, "rev-parse", "HEAD").strip()
    gateway, secret, session = await _gateway(repo)

    out = await _lifecycle(gateway, secret, session, repo)

    assert out["verified"]["ok"] is True, out["verified"]
    result = out["verified"]["result"]
    worktree = Path(result["path"])
    assert ".veya/worktrees" in str(worktree)

    # The verified SHA is the one commit produced, and git agrees.
    sha = out["committed"]["result"]["commit_sha"]
    assert result["commit_sha"] == sha
    assert sha == git(worktree, "rev-parse", "HEAD").strip()
    assert result["parent_sha"] == git(worktree, "rev-parse", "HEAD^").strip()
    assert result["tree"] == git(worktree, "rev-parse", "HEAD^{tree}").strip()
    assert result["changed_paths"] == ["calc.py"]
    assert result["verified"] is True

    # Canonical never moved.
    assert git(repo, "rev-parse", "HEAD").strip() == canonical_head
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"


async def test_the_gateway_refuses_a_sha_from_another_repository(tmp_path: Path) -> None:
    repo_a = seed(tmp_path / "a")
    gateway_a, secret_a, session_a = await _gateway(repo_a)
    out_a = await _lifecycle(gateway_a, secret_a, session_a, repo_a)
    foreign = out_a["committed"]["result"]["commit_sha"]

    repo_b = seed(tmp_path / "b")
    gateway_b, secret_b, session_b = await _gateway(repo_b)
    await _call(
        gateway_b,
        secret_b,
        session_b,
        "file.write",
        {
            "workspace": str(repo_b),
            "execution_target": "NEW_ISOLATED_WORKTREE",
            "path": "calc.py",
            "content": "x = 1\n",
        },
    )

    envelope = await _call(
        gateway_b,
        secret_b,
        session_b,
        "git.verify",
        {
            "workspace": str(repo_b),
            "execution_target": "NEW_ISOLATED_WORKTREE",
            "commit_sha": foreign,
            "expect_paths": ["calc.py"],
        },
    )

    assert envelope["ok"] is False, envelope
    assert envelope["error_code"] == "POLICY_BLOCKED", envelope


async def test_the_gateway_refuses_a_canonical_verify(tmp_path: Path) -> None:
    repo = seed(tmp_path)
    canonical_head = git(repo, "rev-parse", "HEAD").strip()
    gateway, secret, session = await _gateway(repo)
    await _lifecycle(gateway, secret, session, repo)

    envelope = await _call(
        gateway,
        secret,
        session,
        "git.verify",
        {
            "workspace": str(repo),
            "execution_target": "CANONICAL_WORKTREE",
            "commit_sha": git(repo, "rev-parse", "HEAD").strip(),
        },
    )

    assert envelope["ok"] is False, envelope
    assert envelope["error_code"] == "POLICY_BLOCKED", envelope
    assert git(repo, "rev-parse", "HEAD").strip() == canonical_head


async def test_the_gateway_refuses_an_unexpected_path_set(tmp_path: Path) -> None:
    repo = seed(tmp_path)
    gateway, secret, session = await _gateway(repo)
    isolated = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}
    await _call(
        gateway,
        secret,
        session,
        "file.write",
        {**isolated, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
    )
    await _call(gateway, secret, session, "git.stage", {**isolated, "paths": ["calc.py"]})
    committed = await _call(
        gateway,
        secret,
        session,
        "git.commit",
        {**isolated, "message": "fix add", "expect_paths": ["calc.py"]},
    )

    envelope = await _call(
        gateway,
        secret,
        session,
        "git.verify",
        {
            **isolated,
            "commit_sha": committed["result"]["commit_sha"],
            "expect_paths": ["README.md"],
        },
    )

    assert envelope["ok"] is False, envelope
    assert envelope["error_code"] == "POLICY_BLOCKED", envelope


async def test_verify_checks_the_worktree_it_was_asked_about(tmp_path: Path) -> None:
    """Two worktrees exist; the commit belongs to exactly one of them.

    A verifier that resolved "the" worktree by recency rather than by the
    requested target would answer about the other one — and could report a
    commit as verified in a tree it was never committed to.
    """
    repo = seed(tmp_path)
    gateway, secret, session = await _gateway(repo)
    first = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}

    await _call(
        gateway, secret, session, "file.write", {**first, "path": "first.txt", "content": "one\n"}
    )
    await _call(gateway, secret, session, "git.stage", {**first, "paths": ["first.txt"]})
    committed = await _call(
        gateway,
        secret,
        session,
        "git.commit",
        {**first, "message": "first only", "expect_paths": ["first.txt"]},
    )
    sha = committed["result"]["commit_sha"]
    first_worktree = Path(committed["result"]["path"])

    # A second worktree, with a different commit, becomes the most recent one.
    # A second worktree, with a different commit.
    # It is created directly rather than through the gateway, because the
    # gateway's own isolated target is by definition the *first* worktree —
    # there is no second isolated target to ask for.
    manager = WorktreeManager(repo)
    second_worktree = Path(manager.create("task_second", "second").path)
    (second_worktree / "second.txt").write_text("two\\n", encoding="utf-8")
    manager.stage(second_worktree, paths=["second.txt"])
    other_sha = manager.commit(second_worktree, "second only", expect_paths=["second.txt"])[
        "commit_sha"
    ]

    assert second_worktree != first_worktree
    assert other_sha != sha
    # The second worktree's HEAD is its own commit, so resolving "the worktree"
    # by recency would answer about this one instead.
    assert git(second_worktree, "rev-parse", "HEAD").strip() == other_sha

    # Asking about the first worktree's commit must report the first worktree,
    # and must not claim the second one's paths.
    verified = await _call(
        gateway,
        secret,
        session,
        "git.verify",
        {**first, "commit_sha": sha, "expect_paths": ["first.txt"]},
    )
    assert verified["ok"] is True, verified
    assert Path(verified["result"]["path"]) == first_worktree
    assert verified["result"]["changed_paths"] == ["first.txt"]
    assert verified["result"]["commit_sha"] == sha

    # And the first worktree still holds that commit, per the CLI oracle.
    assert git(first_worktree, "rev-parse", "HEAD").strip() == sha
    # The second worktree was never asked about and never moved.
    assert git(second_worktree, "rev-parse", "HEAD").strip() == other_sha


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
