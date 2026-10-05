"""P0-M: a governed git commit surface.

`git.stage` and `git.commit` exist because main had no way to record a change:
the governed git tools were status, diff, log and promote, and promote is
explicitly not a commit.

These tests try the ways that surface could become a way to touch someone's
canonical tree — an absolute path, an upward path, the workspace root itself, an
index holding something the caller did not expect — and require each to fail
closed. The commit SHA is required to be the one git reports, compared against
`git rev-parse` run independently of the code under test.
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
PY = sys.executable
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED", "TIMED_OUT"}


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture()
def workspace(tmp_path: Path) -> tuple[Path, WorktreeManager, Path]:
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "README.md").write_text("# p\n", encoding="utf-8")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")

    manager = WorktreeManager(repo)
    record = manager.create("task_p0m", "p0m")
    return repo, manager, Path(record.path)


# ── the surface exists and is bound to the canonical tools ────────────
def test_both_tools_are_bound_to_dedicated_canonical_tools() -> None:
    from veya.remote.tool_adapter import BINDING_INDEX

    assert BINDING_INDEX["git.stage"].veya_tool == "coding_git_stage"
    assert BINDING_INDEX["git.commit"].veya_tool == "coding_git_commit"
    # Neither borrows the shell, and neither is a promotion.
    for name in ("git.stage", "git.commit"):
        assert "shell" not in (BINDING_INDEX[name].veya_tool or "").lower()


def test_promote_is_untouched_and_still_not_a_commit() -> None:
    from veya.remote.tool_adapter import BINDING_INDEX

    assert BINDING_INDEX["git.promote"].veya_tool == "git_promote"
    root = Path(__file__).resolve().parents[2]
    text = (root / "veya" / "remote" / "git_promotion.py").read_text(encoding="utf-8")
    assert "Promotion != Commit" in text
    assert "NO auto-commit, NO auto-push" in text


def test_the_two_tools_are_added_and_nothing_else_moved() -> None:
    """P0-M's two tools are intact, and the count is stated rather than assumed.

    The count has since moved again for a documented reason (P0-N added
    ``git.verify``), so the assertion is restated instead of relaxed. The
    original purpose stands: a phase that adds a tool without saying so is the
    failure this catches, and a *later* phase's addition must not be able to
    quietly satisfy this one.
    """
    from veya.remote.tool_adapter import BINDINGS

    names = [binding.name for binding in BINDINGS]
    assert len(names) == 45
    assert names.count("git.stage") == 1
    assert names.count("git.commit") == 1
    assert len(set(names)) == len(names), "duplicate binding names"
    # P0-M's surface is exactly the git group it introduced, and P0-N's addition
    # is the only thing that has moved since.
    assert {n for n in names if n.startswith("git.")} == {
        "git.status", "git.diff", "git.stage", "git.commit",
        "git.verify", "git.log", "git.promote",
    }


# ── happy path, with the SHA verified independently ────────────────────
def test_stage_and_commit_in_an_isolated_worktree(workspace) -> None:
    _repo, manager, worktree = workspace
    (worktree / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    staged = manager.stage(worktree, paths=["calc.py"])
    assert staged["staged_paths"] == ["calc.py"]
    assert Path(staged["path"]) == worktree

    committed = manager.commit(worktree, "fix add", expect_paths=["calc.py"])

    # The SHA is git's, not ours: compared against an independent rev-parse.
    assert committed["commit_sha"] == git(worktree, "rev-parse", "HEAD").strip()
    assert committed["parent_sha"] == git(worktree, "rev-parse", "HEAD^").strip()
    assert committed["tree"] == git(worktree, "rev-parse", "HEAD^{tree}").strip()
    assert committed["staged_paths"] == ["calc.py"]
    assert committed["message"] == "fix add"
    # And it is a real commit with the real content.
    subject = git(worktree, "log", "-1", "--pretty=%s").strip()
    assert subject == "fix add"


def test_the_commit_lands_in_the_worktree_not_the_canonical_branch(
    workspace,
) -> None:
    repo, manager, worktree = workspace
    canonical_head = git(repo, "rev-parse", "HEAD").strip()
    canonical_branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()

    (worktree / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    manager.stage(worktree, paths=["calc.py"])
    committed = manager.commit(worktree, "worktree only")

    assert Path(committed["path"]) == worktree
    assert Path(committed["path"]) != repo
    # Canonical HEAD and branch did not move, and canonical content is untouched.
    assert git(repo, "rev-parse", "HEAD").strip() == canonical_head
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == canonical_branch
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"


# ── the canonical tree is not a valid target ──────────────────────────
def test_the_workspace_root_cannot_be_staged_or_committed(workspace) -> None:
    repo, manager, _worktree = workspace

    with pytest.raises(WorktreeError):
        manager.stage(repo)
    with pytest.raises(WorktreeError):
        manager.commit(repo, "should not happen")


def test_an_unregistered_directory_cannot_be_staged(workspace) -> None:
    _repo, manager, worktree = workspace
    outsider = worktree.parent / "not-a-worktree"
    outsider.mkdir()

    with pytest.raises(WorktreeError):
        manager.stage(outsider)


# ── out-of-target paths are refused, not normalised ──────────────────
@pytest.mark.parametrize(
    "bad",
    ["/etc/passwd", "../outside.txt", "sub/../../escape.txt", "-rf", ""],
)
def test_absolute_and_upward_paths_are_refused(workspace, bad: str) -> None:
    """The validator must refuse, not merely fail somewhere downstream.

    Asserting only that ``stage`` raises is not enough: git itself rejects a
    pathspec that matches nothing, so removing the validator still produced an
    error and the test stayed green. The refusal has to come from the rule, so
    the rule is called directly here.
    """
    _repo, manager, worktree = workspace

    with pytest.raises(WorktreeError, match=r"stay inside the worktree|non-empty"):
        manager._relative_paths([bad])

    with pytest.raises(WorktreeError):
        manager.stage(worktree, paths=[bad])


def test_the_path_validator_runs_before_any_git_call(workspace) -> None:
    """A refused path must never reach git.

    ``_relative_paths`` is the gate; if it is bypassed the harm is done by the
    time git runs, so the gate is asserted on its own rather than through a
    side effect.
    """
    _repo, manager, _worktree = workspace

    assert manager._relative_paths(None) == []
    assert manager._relative_paths("ok.txt") == ["ok.txt"]
    assert manager._relative_paths(["a.txt", "b.txt"]) == ["a.txt", "b.txt"]

    for bad in ("/abs", "..", "a/../../b", "-x"):
        with pytest.raises(WorktreeError):
            manager._relative_paths([bad])
    with pytest.raises(WorktreeError):
        manager._relative_paths([None])


def test_a_nul_byte_in_a_path_is_refused(workspace) -> None:
    _repo, manager, worktree = workspace

    with pytest.raises(WorktreeError):
        manager.stage(worktree, paths=["ok.txt\x00"])


# ── commit fails closed rather than committing the unexpected ─────────
def test_an_empty_index_is_refused(workspace) -> None:
    _repo, manager, worktree = workspace

    with pytest.raises(WorktreeError, match="nothing staged"):
        manager.commit(worktree, "empty")


def test_an_unexpected_staged_set_is_refused(workspace) -> None:
    _repo, manager, worktree = workspace
    (worktree / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (worktree / "extra.txt").write_text("surprise\n", encoding="utf-8")
    manager.stage(worktree)

    with pytest.raises(WorktreeError, match="do not match"):
        manager.commit(worktree, "wrong set", expect_paths=["calc.py"])

    # Nothing was committed.
    assert "wrong set" not in git(worktree, "log", "--oneline")


def test_an_empty_message_is_refused(workspace) -> None:
    _repo, manager, worktree = workspace
    (worktree / "calc.py").write_text("x\n", encoding="utf-8")
    manager.stage(worktree)

    with pytest.raises(WorktreeError, match="message"):
        manager.commit(worktree, "   ")


# ── the canonical tools report failure rather than raising ────────────
def test_the_canonical_tool_returns_failed_status_not_an_exception(workspace) -> None:
    from runtime.coding.tools import coding_git_commit, coding_git_stage

    repo, _manager, _worktree = workspace

    staged = coding_git_stage(str(repo), ["calc.py"])
    assert staged["status"] == "failed"
    assert "git stage failed" in json.dumps(staged, default=str)

    committed = coding_git_commit(str(repo), "nope")
    assert committed["status"] == "failed"
    assert "git commit failed" in json.dumps(committed, default=str)


# ── end to end through the gateway, on an isolated target ─────────────
class Executor:
    def __init__(self) -> None:
        self.names: list[str] = []

    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        self.names.append(name)
        if name == "coding_worktree_create":
            manager = WorktreeManager(kwargs["workspace_path"])
            record = manager.create(str(kwargs["task_id"]), str(kwargs.get("objective") or "p0m"))
            return json.dumps({"status": "ok", "data": {"worktree": record.to_dict()}}, default=str)
        if name == "write_file":
            from server.tool_registry import _tool_write_file

            return json.dumps({"status": "ok", "data": {"result": str(_tool_write_file(
                str(kwargs.get("filepath")), str(kwargs.get("content", "")), True))}})
        return json.dumps({"status": "ok", "data": {"stdout": "unused"}})


def adapter_session_worktrees(gateway) -> dict:
    """The session-scoped worktree map the fast-git path would consult."""
    sessions = gateway.sessions
    for session_id in list(getattr(sessions, "_by_id", {}) or {}):
        session = sessions.require(session_id)
        if session.worktrees:
            return dict(session.worktrees)
    return {}


async def _open(tmp_path: Path, repo: Path):
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
    session = (await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"workspace": str(repo)}},
        authorization=f"Bearer {secret}",
    ))["result"]["sessionId"]
    return gateway, secret, session


async def _call(gateway, secret, session, name, arguments):
    response = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}},
        authorization=f"Bearer {secret}", session_header=session,
    )
    return response["result"]["structuredContent"]


async def test_stage_and_commit_through_the_gateway(tmp_path: Path) -> None:
    repo, _manager, _worktree = _seed(tmp_path)
    canonical_head = git(repo, "rev-parse", "HEAD").strip()
    gateway, secret, session = await _open(tmp_path, repo)

    # Create the isolated worktree through a governed mutation.
    write = await _call(gateway, secret, session, "file.write", {
        "path": "calc.py", "content": "def add(a, b):\n    return a + b\n",
        "workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE",
    })
    assert write["ok"] is True, write

    staged = await _call(gateway, secret, session, "git.stage", {
        "workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE",
        "paths": ["calc.py"],
    })
    assert staged["ok"] is True, staged
    assert staged["result"]["staged_paths"] == ["calc.py"]

    committed = await _call(gateway, secret, session, "git.commit", {
        "workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE",
        "message": "fix add", "expect_paths": ["calc.py"],
    })
    assert committed["ok"] is True, committed

    payload = committed["result"]
    worktree = Path(payload["path"])
    assert worktree != repo
    assert ".veya/worktrees" in str(worktree)
    assert payload["commit_sha"] == git(worktree, "rev-parse", "HEAD").strip()
    assert payload["staged_paths"] == ["calc.py"]
    # Canonical untouched.
    assert git(repo, "rev-parse", "HEAD").strip() == canonical_head
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"


async def test_the_explicit_target_is_what_reaches_the_manager(tmp_path: Path) -> None:
    """The execution target must be load-bearing, not decorative.

    In the happy path above a prior file.write had already registered a session
    worktree, so ``_direct_workdir`` would have found the same tree whether or not
    the explicit target was resolved — which is why bypassing that resolution
    changed nothing. Here the session has no worktree at all, so the only way to
    reach an isolated tree is to honour execution_target. If the resolution is
    skipped, the canonical root is used and the manager refuses.
    """
    repo, _manager, _worktree = _seed(tmp_path)
    gateway, secret, session = await _open(tmp_path, repo)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    # The fixture created a worktree on disk, but this *session* has none
    # registered, so _direct_workdir would hand back the canonical root. Only
    # honouring execution_target can reach an isolated tree from here.
    assert not adapter_session_worktrees(gateway), "session should start with none"

    staged = await _call(gateway, secret, session, "git.stage", {
        "workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE",
        "paths": ["calc.py"],
    })

    assert staged["ok"] is True, staged
    worktree = Path(staged["result"]["path"])
    assert worktree != repo
    assert ".veya/worktrees" in str(worktree)


async def test_the_gateway_refuses_a_canonical_commit(tmp_path: Path) -> None:
    repo, _manager, _worktree = _seed(tmp_path)
    canonical_head = git(repo, "rev-parse", "HEAD").strip()
    gateway, secret, session = await _open(tmp_path, repo)

    staged = await _call(gateway, secret, session, "git.stage", {
        "workspace": str(repo), "execution_target": "CANONICAL_WORKTREE", "paths": ["calc.py"],
    })
    assert staged["ok"] is False, staged
    assert staged["error_code"] == "POLICY_BLOCKED", staged
    assert git(repo, "rev-parse", "HEAD").strip() == canonical_head


async def test_the_gateway_refuses_an_empty_commit(tmp_path: Path) -> None:
    repo, _manager, _worktree = _seed(tmp_path)
    gateway, secret, session = await _open(tmp_path, repo)
    await _call(gateway, secret, session, "file.write", {
        "path": "calc.py", "content": "def add(a, b):\n    return a + b\n",
        "workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE",
    })
    await _call(gateway, secret, session, "git.stage", {
        "workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE", "paths": ["README.md"],
    })
    await _call(gateway, secret, session, "git.commit", {
        "workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE", "paths": [],
        "message": "nothing to say",
    })

    envelope = await _call(gateway, secret, session, "git.commit", {
        "workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE",
        "message": "should fail closed",
    })

    assert envelope["ok"] is False, envelope
    assert envelope["error_code"] == "POLICY_BLOCKED", envelope


def _seed(tmp_path: Path) -> tuple[Path, WorktreeManager, Path]:
    repo = tmp_path / "seeded"
    repo.mkdir()
    (repo / "README.md").write_text("# p\n", encoding="utf-8")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    manager = WorktreeManager(repo)
    record = manager.create("task_p0m_e2e", "p0m")
    return repo, manager, Path(record.path)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
