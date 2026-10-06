"""SR-002 + SR-004 — the two seams with zero closure coupling.

Scope is deliberately narrow:

* SR-002  ``CURRENT_SESSION_WORKTREE`` exists and resolves to the *exact*
  session worktree, never the canonical root, cwd, or an execution_id.
* SR-004  a directory that is not a Git working tree is refused instead of
  resolving upward into its parent repository.

Explicitly OUT of scope and untouched here: SR-001 (canonical/session
substitution), SR-003 (dead-worktree fallback), SR-005 (execution_id
precedence, which stays byte-identical).

Routing oracle
--------------
Routing assertions use ``sentinel_worktree_only.txt``, a file that exists
**only inside the session worktree**. A file present in both trees (such as
``calc.py``) cannot prove routing and is never used for that purpose.

Error-code note: the stable taxonomy in ``veya/remote/models.py`` has no
``TARGET_INVALID`` member. Both refusals therefore use ``POLICY_BLOCKED``,
consistent with the existing dead-worktree refusal, rather than inventing a new
public error code.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar

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

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)
SENTINEL = "sentinel_worktree_only.txt"
SENTINEL_BODY = "SESSION-WORKTREE-ONLY\n"


def git(path: Path | str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    ).stdout.strip()


class Executor:
    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        if name == "coding_worktree_create":
            from runtime.coding.worktree import WorktreeManager

            record = WorktreeManager(kwargs["workspace_path"]).create(
                str(kwargs["task_id"]), str(kwargs.get("objective") or "sr002-sr004")
            )
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
                },
                default=str,
            )
        return json.dumps({"status": "ok", "data": {}})


class Routing:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    async def __aenter__(self) -> Routing:
        auth = RemoteAuth()
        _r, self.secret = auth.issue("sr002", permissions=PERMS, workspaces=[str(self.repo)])
        audit = RemoteAudit(None)
        self.adapter = RemoteToolAdapter(
            Executor(), redact=audit.redact, execution_store=ExecutionStore(None)
        )
        self.gateway = create_gateway(
            auth=auth,
            sessions=RemoteSessionManager(ttl_s=300, max_sessions=16),
            audit=audit,
            adapter=self.adapter,
        )
        response = await self.gateway.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"workspace": str(self.repo)},
            },
            authorization=f"Bearer {self.secret}",
        )
        self.session = response["result"]["sessionId"]
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = await self.gateway.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            authorization=f"Bearer {self.secret}",
            session_header=self.session,
        )
        return response["result"]["structuredContent"]

    def args(self, target: str, **extra: Any) -> dict[str, Any]:
        return {"workspace": str(self.repo), "execution_target": target, **extra}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "."],
        ["commit", "-qm", "baseline"],
    ):
        git(root, *cmd)
    return root


async def _session_worktree_with_sentinel(rt: Routing) -> Path:
    """Create W through the governed path and plant the W-only sentinel."""

    wrote = await rt.tool(
        "file.write", rt.args("NEW_ISOLATED_WORKTREE", path=SENTINEL, content=SENTINEL_BODY)
    )
    assert wrote["ok"] is True, wrote
    text = (wrote.get("result") or {}).get("text", "")
    match = re.search(r"(/[^\s]+/\.veya/worktrees/task-[^\s]+)/" + re.escape(SENTINEL), text)
    assert match, text
    worktree = Path(match.group(1))
    assert (worktree / SENTINEL).read_text() == SENTINEL_BODY
    # The oracle only holds if canonical does NOT have it.
    assert not (rt.repo / SENTINEL).exists()
    return worktree


# ══════════════ SR-002 ══════════════
def test_sr002_target_is_a_recognised_identifier() -> None:
    """SR2.1a: the identifier is accepted as a legal execution target."""

    from veya.remote.tool_adapter import EXECUTION_TARGETS

    assert "CURRENT_SESSION_WORKTREE" in EXECUTION_TARGETS


async def test_sr2_1_session_worktree_exists_resolves_exact_worktree(repo: Path) -> None:
    """SR2.1: resolves to the exact worktree root -- not canonical, not cwd."""

    rt = Routing(repo)
    async with rt:
        worktree = await _session_worktree_with_sentinel(rt)

        envelope = await rt.tool("git.status", rt.args("CURRENT_SESSION_WORKTREE"))
        assert envelope["ok"] is True, envelope
        cwd = (envelope.get("result") or {}).get("cwd")
        assert cwd, "a resolved target must report the cwd it used"
        assert Path(cwd).resolve() == worktree.resolve(), (
            f"CURRENT_SESSION_WORKTREE must resolve to the worktree, got {cwd}",
        )
        # Not the canonical root, and not a subdirectory of it.
        assert Path(cwd).resolve() != repo.resolve()

        # The sentinel is visible from that resolved root only.
        assert (Path(cwd) / SENTINEL).read_text() == SENTINEL_BODY
        assert not (repo / SENTINEL).exists()


async def test_sr2_2_no_session_worktree_is_invalid_not_fallback(repo: Path) -> None:
    """SR2.2: with no session worktree the target is invalid, never canonical."""

    rt = Routing(repo)
    async with rt:
        # No file.write first: the session has no registered worktree.
        envelope = await rt.tool("git.status", rt.args("CURRENT_SESSION_WORKTREE"))
        assert envelope["ok"] is False, (
            "SR-002: CURRENT_SESSION_WORKTREE silently fell back instead of "
            "reporting an invalid target",
            envelope,
        )
        assert envelope.get("error_code") == "POLICY_BLOCKED", envelope
        assert "CURRENT_SESSION_WORKTREE" in str(envelope.get("message")), envelope


async def test_sr002_resolver_does_not_fall_back_to_canonical_or_cwd(tmp_path: Path) -> None:
    """Unit-level: the resolver refuses rather than degrading to a default."""

    from veya.remote.tool_adapter import RemoteToolAdapter, RemoteToolAdapterError

    class _Binding:
        repo_root = str(tmp_path)
        requested_realpath = str(tmp_path)

    class _Session:
        worktrees: ClassVar[dict[str, str]] = {}

    adapter = RemoteToolAdapter.__new__(RemoteToolAdapter)
    with pytest.raises(RemoteToolAdapterError) as excinfo:
        adapter._direct_workdir(
            _Session(),  # type: ignore[arg-type]
            _Binding(),  # type: ignore[arg-type]
            execution_target="CURRENT_SESSION_WORKTREE",
        )
    assert excinfo.value.code == "POLICY_BLOCKED"


# ══════════════ SR-004 ══════════════
def test_sr4_1_empty_directory_is_invalid(tmp_path: Path) -> None:
    from veya.remote.tool_adapter import _invalid_git_target_reason

    empty = tmp_path / "empty"
    empty.mkdir()
    assert _invalid_git_target_reason(empty) is not None, (
        "SR-004: an empty directory must not pass as a git target"
    )


def test_sr4_2_random_non_git_directory_is_invalid(tmp_path: Path) -> None:
    from veya.remote.tool_adapter import _invalid_git_target_reason

    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    (plain / "readme.txt").write_text("hi", encoding="utf-8")
    assert _invalid_git_target_reason(plain) is not None


def test_sr4_2b_non_git_directory_inside_a_repo_does_not_resolve_upward(tmp_path: Path) -> None:
    """The parent IS a repository; that must not lend validity to the child."""

    from veya.remote.tool_adapter import _invalid_git_target_reason

    root = tmp_path / "proj"
    root.mkdir()
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        git(root, *cmd)
    child = root / "subdir"
    child.mkdir()
    assert git(root, "rev-parse", "--is-inside-work-tree") == "true"
    assert _invalid_git_target_reason(child) is not None, (
        "SR-004: resolved upward into the parent repository"
    )


def test_sr4_3_valid_linked_worktree_passes(tmp_path: Path) -> None:
    """A linked worktree's .git is a FILE and must still be accepted."""

    from veya.remote.tool_adapter import _invalid_git_target_reason

    root = tmp_path / "proj"
    root.mkdir()
    (root / "f.txt").write_text("x", encoding="utf-8")
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "."],
        ["commit", "-qm", "b"],
    ):
        git(root, *cmd)

    linked = tmp_path / "linked"
    git(root, "worktree", "add", "-q", str(linked), "-b", "side")
    assert (linked / ".git").is_file(), "linked worktrees use a .git FILE"
    assert _invalid_git_target_reason(linked) is None
    assert git(linked, "rev-parse", "--is-inside-work-tree") == "true"


def test_sr4_4_canonical_repository_passes(tmp_path: Path) -> None:
    from veya.remote.tool_adapter import _invalid_git_target_reason

    root = tmp_path / "proj"
    root.mkdir()
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        git(root, *cmd)
    assert _invalid_git_target_reason(root) is None


def test_sr4_5_missing_path_still_refused(tmp_path: Path) -> None:
    from veya.remote.tool_adapter import _invalid_git_target_reason

    assert _invalid_git_target_reason(tmp_path / "nope") is not None


async def test_sr4_end_to_end_empty_workspace_is_refused(tmp_path: Path) -> None:
    """Through the gateway: an empty workspace must not yield canonical state."""

    root = tmp_path / "proj"
    root.mkdir()
    (root / "f.txt").write_text("x", encoding="utf-8")
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "."],
        ["commit", "-qm", "b"],
    ):
        git(root, *cmd)
    empty = root / "empty_child"
    empty.mkdir()

    rt = Routing(root)
    async with rt:
        envelope = await rt.tool(
            "git.status", {"workspace": str(empty), "execution_target": "CANONICAL_WORKTREE"}
        )
        assert envelope["ok"] is False, envelope
        assert envelope.get("error_code"), envelope
        # It must not report the parent's repository state.
        assert str(root) not in str(envelope.get("result") or {}), envelope


def test_sr004_never_searches_parents(tmp_path: Path) -> None:
    """Structural guard against reintroducing an upward search."""

    import inspect

    from veya.remote import tool_adapter

    source = inspect.getsource(tool_adapter._invalid_git_target_reason)
    assert ".parents" not in source, "must not walk upward looking for a repository"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_sr4_6_dangling_linked_worktree_pointer_is_invalid(tmp_path: Path) -> None:
    """A .git file pointing at missing metadata must not pass as a worktree."""

    from veya.remote.tool_adapter import _invalid_git_target_reason

    fake = tmp_path / "looks_like_worktree"
    fake.mkdir()
    (fake / ".git").write_text("gitdir: /nonexistent/veya/metadata\n", encoding="utf-8")
    assert _invalid_git_target_reason(fake) is not None, (
        "SR-004: accepted a linked-worktree pointer to missing metadata"
    )


def test_sr4_7_malformed_git_file_is_invalid(tmp_path: Path) -> None:
    from veya.remote.tool_adapter import _invalid_git_target_reason

    fake = tmp_path / "malformed"
    fake.mkdir()
    (fake / ".git").write_text("this is not a gitdir pointer\n", encoding="utf-8")
    assert _invalid_git_target_reason(fake) is not None
