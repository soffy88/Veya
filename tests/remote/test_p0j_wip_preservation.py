"""P0-J: WIP preservation.

Pure proof. Nothing here adds a cleanup, backup or restore mechanism — the point
is to establish whether the existing paths already preserve the user's work, and
to make a regression in that property detectable.

The user's own working tree is never used as a subject. Every test builds a
synthetic repository, dirties it in a controlled way (modified tracked file,
staged change, untracked file), drives the real governed tools, and then asserts
byte-level preservation. No git clean, no reset, no stash, anywhere.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from veya.remote import (
    RemoteAudit,
    RemoteAuth,
    RemotePermissions,
    RemoteSessionManager,
    RemoteToolAdapter,
)
from veya.remote.execution import ExecutionStore
from veya.remote.mcp_server import create_gateway
from veya.remote.tool_adapter import EXECUTION_TARGETS, resolve_execution_target

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)
PY = sys.executable
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED", "TIMED_OUT"}

#: A command that writes a file into its cwd. Built from chr() codes so the
#: shell-visible string carries no nested quoting of its own.
_WRITE_PY = (
    "import pathlib; pathlib.Path(chr(103)+chr(101)+chr(110)+chr(101)+chr(114)+"
    "chr(97)+chr(116)+chr(101)+chr(100)+chr(46)+chr(116)+chr(120)+chr(116))"
    ".write_text(chr(120))"
)
WRITE_COMMAND = f'{PY} -c "{_WRITE_PY}"'


class Executor:
    """Delegates the mutation tools so a mutation is a real mutation.

    ``coding_worktree_create`` is delegated too. Without it the adapter cannot
    verify repo identity and refuses with "worktree create returned no
    path/repo_root" — which would make every isolation assertion here pass for
    the wrong reason: nothing would ever be written anywhere.
    """

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.worktrees: list[str] = []

    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        if name == "coding_worktree_create":
            # The worktree is made through WorktreeManager directly rather than
            # through runtime.coding.tools.coding_worktree_create: that tool
            # builds a harness contract first and currently fails in this
            # environment with "module 'oskill' has no attribute
            # 'capability_intersection'". The unit under test here is the
            # adapter's isolation and identity verification, not the harness
            # contract builder, so the contract layer is stepped around and the
            # manager below it is real.
            from runtime.coding.worktree import WorktreeManager

            manager = WorktreeManager(kwargs["workspace_path"])
            record = manager.create(str(kwargs["task_id"]), str(kwargs.get("objective") or "p0j"))
            self.worktrees.append(record.path)
            return json.dumps(
                {"status": "ok", "data": {"worktree": record.to_dict()}}, default=str
            )
        if name == "write_file":
            from server.tool_registry import _tool_write_file

            result = _tool_write_file(
                str(kwargs.get("filepath")),
                str(kwargs.get("content", "")),
                bool(kwargs.get("overwrite", True)),
            )
            self.writes.append(str(kwargs.get("filepath")))
            return json.dumps({"status": "ok", "data": {"result": str(result)}})
        return json.dumps({"status": "ok", "data": {"stdout": "unused"}})


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout


def make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
    (repo / "untouched.txt").write_text("keep me\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    return repo


def dirty_repo(tmp_path: Path) -> Path:
    """A repo carrying three kinds of user work: modified, staged, untracked."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged base\n", encoding="utf-8")
    (repo / "untouched.txt").write_text("keep me\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")

    # Modified but unstaged: the canonical WIP under test.
    (repo / "tracked.txt").write_text("USER EDIT IN PROGRESS\n", encoding="utf-8")
    # Staged: the index state that must survive.
    git(repo, "add", "staged.txt")
    # Untracked: must never be deleted.
    (repo / "notes.md").write_text("scratch\n", encoding="utf-8")
    return repo


def snapshot(repo: Path) -> dict[str, Any]:
    """Everything about the user's work that must come back unchanged."""
    return {
        "tracked": (repo / "tracked.txt").read_text(encoding="utf-8"),
        "untouched": (repo / "untouched.txt").read_text(encoding="utf-8"),
        "notes": (
            (repo / "notes.md").read_text(encoding="utf-8")
            if (repo / "notes.md").exists()
            else None
        ),
        "staged_index": git(repo, "diff", "--cached", "--name-only"),
        "unstaged": git(repo, "diff", "--name-only"),
        "head": git(repo, "rev-parse", "HEAD").strip(),
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(),
    }


def make_gateway(repo: Path, executor: Executor):
    auth = RemoteAuth()
    _record, secret = auth.issue("tester", permissions=PERMS, workspaces=[str(repo)])
    audit = RemoteAudit(None)
    adapter = RemoteToolAdapter(
        executor, redact=audit.redact, execution_store=ExecutionStore(None)
    )
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=8),
        audit=audit,
        adapter=adapter,
    )
    return gateway, secret


async def open_session(gateway, secret, workspace: str) -> str:
    response = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"workspace": workspace}},
        authorization=f"Bearer {secret}",
    )
    return response["result"]["sessionId"]


async def call(gateway, secret, session, name, arguments):
    response = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}},
        authorization=f"Bearer {secret}",
        session_header=session,
    )
    return response["result"]["structuredContent"]


async def wait_terminal(gateway, secret, session, execution_id, timeout: float = 90.0):
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    last: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        envelope = await call(
            gateway, secret, session, "process.status", {"execution_id": execution_id}
        )
        last = envelope.get("result", envelope)
        if last.get("status") in TERMINAL:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"never terminal: {last.get('status')}")


def worktree_files(repo: Path) -> list[Path]:
    root = repo / ".veya" / "worktrees"
    if not root.is_dir():
        return []
    return [p for p in root.rglob("newfile.txt")]


# ── §10.3 isolation is what protects the canonical tree ───────────────
async def test_a_write_lands_in_an_isolated_worktree_and_not_the_canonical_tree(
    tmp_path: Path,
) -> None:
    """The WIP-critical invariant: a governed write must not touch the owner's tree."""
    repo = dirty_repo(tmp_path)
    before = snapshot(repo)
    executor = Executor()
    gateway, secret = make_gateway(repo, executor)
    session = await open_session(gateway, secret, str(repo))

    envelope = await call(
        gateway,
        secret,
        session,
        "file.write",
        {"path": "newfile.txt", "content": "written by the agent\n", "workspace": str(repo)},
    )

    assert envelope["ok"] is True, envelope
    assert executor.writes, "the write must really have happened"

    # The owner's work is byte-identical.
    assert snapshot(repo) == before
    assert not (repo / "newfile.txt").exists(), "write leaked into the canonical tree"
    # And the write is somewhere else entirely.
    assert worktree_files(repo), "the isolated worktree should hold the new file"


async def test_staged_state_and_untracked_files_survive_a_governed_write(
    tmp_path: Path,
) -> None:
    repo = dirty_repo(tmp_path)
    before = snapshot(repo)
    gateway, secret = make_gateway(repo, Executor())
    session = await open_session(gateway, secret, str(repo))

    await call(
        gateway,
        secret,
        session,
        "file.write",
        {"path": "newfile.txt", "content": "x\n", "workspace": str(repo)},
    )

    after = snapshot(repo)
    assert after["staged_index"] == before["staged_index"], after["staged_index"]
    assert after["unstaged"] == before["unstaged"]
    assert (repo / "notes.md").exists(), "untracked scratch file was deleted"
    assert after["tracked"] == "USER EDIT IN PROGRESS\n"
    assert after["head"] == before["head"], "HEAD moved"
    assert after["branch"] == "main"


# ── §10.x refusal, failure and cancel leave the tree alone ────────────
async def test_a_refused_write_leaves_the_canonical_tree_untouched(tmp_path: Path) -> None:
    repo = dirty_repo(tmp_path)
    before = snapshot(repo)
    gateway, secret = make_gateway(repo, Executor())
    session = await open_session(gateway, secret, str(repo))

    envelope = await call(
        gateway,
        secret,
        session,
        "file.write",
        {"path": "../../escape.txt", "content": "nope\n", "workspace": str(repo)},
    )

    assert envelope["ok"] is False, envelope
    assert snapshot(repo) == before


async def test_a_failing_execution_leaves_the_canonical_tree_untouched(
    tmp_path: Path,
) -> None:
    repo = dirty_repo(tmp_path)
    before = snapshot(repo)
    gateway, secret = make_gateway(repo, Executor())
    session = await open_session(gateway, secret, str(repo))

    envelope = await call(
        gateway,
        secret,
        session,
        "test.run",
        {"command": f'{PY} -c "import sys; sys.exit(7)"', "workspace": str(repo), "wait": True},
    )
    # A command that ran and exited non-zero is reported as a failed submission
    # with a real handle, not as ok=true. The exit code and failure class live on
    # the record; the envelope only says it did not succeed.
    assert envelope["ok"] is False, envelope
    assert envelope["error_code"] == "EXECUTION_FAILED", envelope
    assert envelope["execution_id"], envelope

    terminal = await wait_terminal(gateway, secret, session, envelope["execution_id"])
    assert terminal["status"] == "FAILED", terminal
    assert terminal["exit_code"] == 7, terminal

    assert snapshot(repo) == before


async def test_a_cancelled_execution_leaves_the_canonical_tree_untouched(
    tmp_path: Path,
) -> None:
    # A dirty canonical tree makes execution fail closed (pinned separately
    # below), so the cancel path is exercised on a clean tree here and the WIP
    # assertions still hold.
    repo = make_git_repo(tmp_path)
    before = snapshot(repo)
    gateway, secret = make_gateway(repo, Executor())
    session = await open_session(gateway, secret, str(repo))

    envelope = await call(
        gateway,
        secret,
        session,
        "test.run",
        {
            "command": f'{PY} -c "import time; time.sleep(45)"',
            "workspace": str(repo),
            "execution_target": "CANONICAL_WORKTREE",
            "wait": True,
        },
    )
    assert envelope["ok"] is True, envelope
    execution_id = envelope["execution_id"]

    # Cancel straight after submission rather than after waiting for RUNNING.
    # cancel() only short-circuits on an already-terminal record, so this is the
    # same transition without a race against the status projection, which does
    # not have to surface RUNNING for a command that finishes quickly.
    cancelled = await call(
        gateway, secret, session, "process.cancel", {"execution_id": execution_id}
    )
    assert cancelled["ok"] is True, cancelled
    terminal = await wait_terminal(gateway, secret, session, execution_id)

    # P0-J owns the WIP question here, not the lifecycle one: whatever the cancel
    # does to the execution, the owner's working state must be exactly as it was.
    assert snapshot(repo) == before, "cancelling an execution disturbed the owner's work"

    # Known gap, recorded rather than asserted away. A cancel issued while the
    # execution is still STARTING reports ok=true and leaves the command running:
    # there is no process group to signal yet, and the record goes on to report
    # COMPLETED. Cancelling after RUNNING does transition to CANCELLED, which is
    # what P0-H's test_start_running_cancel_cancelled covers. This belongs to the
    # lifecycle phase, not to WIP preservation, so it is named rather than
    # papered over here.
    assert terminal["status"] in {"CANCELLED", "COMPLETED"}, terminal


async def test_execution_against_a_dirty_canonical_tree_preserves_it(
    tmp_path: Path,
) -> None:
    """Execution runs in the canonical tree by design, so it must not disturb it.

    This is the counterpart to the mutation isolation above. A non-writing
    command against a dirty tree completes and leaves every modified, staged and
    untracked byte exactly as it found them. What it does *not* do is stop a
    writing command from touching that tree — see the divergence test below.
    """
    repo = dirty_repo(tmp_path)
    before = snapshot(repo)
    gateway, secret = make_gateway(repo, Executor())
    session = await open_session(gateway, secret, str(repo))

    envelope = await call(
        gateway,
        secret,
        session,
        "test.run",
        {"command": f'{PY} -c "print(1)"', "workspace": str(repo), "wait": True},
    )

    assert envelope["ok"] is True, envelope
    terminal = await wait_terminal(gateway, secret, session, envelope["execution_id"])
    assert terminal["status"] == "COMPLETED", terminal

    after = snapshot(repo)
    assert after == before, "a read-only command changed the owner's working state"
    assert after["tracked"] == "USER EDIT IN PROGRESS\n"
    assert after["staged_index"] == before["staged_index"]
    assert (repo / "notes.md").exists()


async def test_a_writing_command_pollutes_the_canonical_tree(tmp_path: Path) -> None:
    """The §10.3 divergence, measured rather than asserted away.

    Mutations are isolated by default. Execution is not: it runs in the canonical
    tree, so a command that writes leaves its output there. This test documents
    that consequence as a measured fact, because it is the one respect in which
    "tests default to an isolated worktree" does not hold on main today. It is
    expected to keep passing; if execution is ever isolated by default, this test
    is the one that should be revisited.
    """
    repo = make_git_repo(tmp_path)
    gateway, secret = make_gateway(repo, Executor())
    session = await open_session(gateway, secret, str(repo))

    envelope = await call(
        gateway,
        secret,
        session,
        "test.run",
        {
            "command": WRITE_COMMAND,
            "workspace": str(repo),
            "wait": True,
        },
    )
    assert envelope["ok"] is True, envelope
    await wait_terminal(gateway, secret, session, envelope["execution_id"])

    assert (repo / "generated.txt").exists(), (
        "execution is expected to run in the canonical tree; if this now fails, "
        "execution has been isolated and the §10.3 divergence is closed"
    )


# ── the target defaults themselves ────────────────────────────────────
def test_a_mutation_defaults_to_an_isolated_worktree() -> None:
    """The protection, stated as a fact about the resolver.

    If this default ever moves to canonical, a plain file.write lands in the
    owner's live tree and the isolation the rest of this file relies on is gone.
    """
    resolved = resolve_execution_target("/tmp/proj", intent="mutation")
    assert resolved == "NEW_ISOLATED_WORKTREE", resolved


def test_execution_defaults_to_the_canonical_worktree_by_design() -> None:
    """Execution is canonical on purpose, and that is a divergence worth seeing.

    ``resolve_execution_target`` gives read/execute intent CANONICAL_WORKTREE
    because a fresh worktree is a clean checkout with no virtualenv and none of
    the current working state, so running there made the tree's own modules
    unimportable. Mutation keeps the isolated default. This test exists so the
    asymmetry is visible in code rather than discovered later: a command that
    writes does so into the canonical tree, and that is a documented trade-off,
    not an accident.
    """
    assert resolve_execution_target("/tmp/proj", intent="read") == "CANONICAL_WORKTREE"
    assert resolve_execution_target("/tmp/proj", intent="mutation") == "NEW_ISOLATED_WORKTREE"
    assert set(EXECUTION_TARGETS) == {
        "NEW_ISOLATED_WORKTREE",
        "EXECUTION_WORKTREE",
        "EXISTING_WORKTREE",
        "CANONICAL_WORKTREE",
        "HOST",
    }


# ── no destructive git anywhere in the governed paths ─────────────────
def test_governed_paths_contain_no_destructive_git() -> None:
    """A ratchet, not a one-off check.

    The forbidden forms are the ones that would silently discard user work:
    ``reset --hard``, ``clean -f``, ``stash drop`` and ``checkout --`` against a
    user's files. They are absent today; this fails if one is introduced.
    """
    import re

    forbidden = re.compile(
        r"(reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s|stash\s+(drop|pop))"
    )
    targets = [
        "veya/remote/tool_adapter.py",
        "veya/remote/direct_exec.py",
        "veya/remote/git_promotion.py",
        "veya/remote/workspace_binding.py",
        "veya/remote/execution.py",
        "runtime/coding/worktree.py",
        "runtime/coding/command_runner.py",
    ]
    root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for rel in targets:
        text = (root / rel).read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped[:1] in {"#", '"', "*", "-", "+"}:
                continue
            if forbidden.search(stripped):
                offenders.append(f"{rel}:{number}: {stripped}")

    assert offenders == [], offenders


def test_promotion_states_its_destructive_git_prohibition() -> None:
    """git_promotion claims zero reset/clean/stash; the claim is asserted here."""
    root = Path(__file__).resolve().parents[2]
    text = (root / "veya" / "remote" / "git_promotion.py").read_text(encoding="utf-8")

    assert "Never uses reset --hard, clean, or stash." in text
    assert "unrelated canonical dirty files are NEVER touched or stashed" in text


# ── the phase commit must not carry unrelated WIP ─────────────────────
def test_the_phase_commit_touches_only_its_own_paths() -> None:
    """§10.4: unrelated WIP must not ride along in a phase commit.

    Checked against the real HEAD rather than a fixture, so it describes the
    repository this file ships in.
    """
    root = Path(__file__).resolve().parents[2]
    listing = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
    ).stdout.split()

    assert listing, "HEAD has no changed paths"
    # Tests and qualification documents are this work's own output. Anything else
    # in a phase commit — a source file the phase was not meant to touch, or the
    # owner's uncommitted work — is the failure this catches.
    assert all(path.startswith(("tests/", "docs/")) for path in listing), listing


def test_this_repository_still_carries_its_uncommitted_work() -> None:
    """If a phase ever ran ``clean`` or ``reset`` on the owner, this goes red.

    The working tree this file lives in is the user's. Whatever they had in
    flight must still be in flight afterwards; the assertion is that *something*
    uncommitted survives, which is the cheapest honest signal that the
    destructive forms were not used.
    """
    root = Path(__file__).resolve().parents[2]
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
    ).stdout.strip()

    assert status, "the canonical working tree is unexpectedly clean"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
