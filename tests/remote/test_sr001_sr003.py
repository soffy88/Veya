"""SR-001 + SR-003 — target substitution and dead-worktree fallback.

Baseline: 15e7a639 (SR-002 + SR-004 already closed).

* SR-001  an explicit ``CANONICAL_WORKTREE`` must resolve to the canonical root
  and must never be answered out of the session worktree.
* SR-003  a dead session worktree must be refused, never degraded to canonical,
  cwd, a parent repository, another worktree, or an execution_id.

SR-005 (``execution_id`` precedence) is OUT OF SCOPE and must not be "fixed"
here; where a test touches that seam it records the state, nothing more.

Routing oracles
---------------
Two sentinels, each present in exactly one target:

* ``canonical_only.txt`` — canonical checkout only
* ``session_only.txt``   — session worktree only

A file present in both trees (``calc.py``) can never prove routing and is never
used as an oracle here.

Every test drives the real governed surface:
``create_gateway`` -> binding -> admission -> target resolution -> tool.
"""

from __future__ import annotations

import json
import re
import shutil
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

CANONICAL_ONLY = "canonical_only.txt"
SESSION_ONLY = "session_only.txt"
CANONICAL_BODY = "CANONICAL-ONLY\n"
SESSION_BODY = "SESSION-ONLY\n"

CANONICAL = "CANONICAL_WORKTREE"
SESSION = "CURRENT_SESSION_WORKTREE"


def git(path: Path | str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    ).stdout.strip()


class Executor:
    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        if name == "coding_worktree_create":
            from runtime.coding.worktree import WorktreeManager

            record = WorktreeManager(kwargs["workspace_path"]).create(
                str(kwargs["task_id"]), str(kwargs.get("objective") or "sr001-sr003")
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
        if name == "search_files":
            from server.tool_registry import _tool_search_files

            return json.dumps(
                {
                    "status": "ok",
                    "data": {
                        "result": str(
                            _tool_search_files(
                                str(kwargs.get("path")), str(kwargs.get("query", ""))
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
        _r, self.secret = auth.issue("sr001", permissions=PERMS, workspaces=[str(self.repo)])
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

    def text_of(self, envelope: dict[str, Any]) -> str:
        return str((envelope.get("result") or {}).get("text", ""))


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    # NOTE: canonical_only.txt is deliberately NOT created here. A file present
    # at commit time is inherited by every linked worktree, so it would exist in
    # both trees and could not prove routing. _two_target_setup creates it after
    # the worktree exists.
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "."],
        ["commit", "-qm", "baseline"],
    ):
        git(root, *cmd)
    return root


async def _two_target_setup(rt: Routing) -> Path:
    """Build two genuinely disjoint trees.

    Order matters: a linked worktree is branched from canonical HEAD, so any
    file committed beforehand exists in BOTH trees and cannot prove routing.
    Therefore the worktree is created first, then canonical_only.txt is written
    into canonical only, and session_only.txt into the worktree only.
    """

    bootstrap = await rt.tool(
        "file.write",
        rt.args("NEW_ISOLATED_WORKTREE", path="bootstrap.txt", content="bootstrap\n"),
    )
    assert bootstrap["ok"] is True, bootstrap
    match = re.search(
        r"(/[^\s]+/\.veya/worktrees/task-[^\s]+)/bootstrap\.txt",
        rt.text_of(bootstrap),
    )
    assert match, rt.text_of(bootstrap)
    worktree = Path(match.group(1))

    # canonical_only.txt: canonical only. Written to the fixture checkout
    # directly so no routing decision can influence where it lands.
    (rt.repo / CANONICAL_ONLY).write_text(CANONICAL_BODY, encoding="utf-8")

    # session_only.txt: session worktree only.
    planted = await rt.tool("file.write", rt.args(SESSION, path=SESSION_ONLY, content=SESSION_BODY))
    assert planted["ok"] is True, planted

    # Both oracles are now unambiguous, and asserted rather than assumed.
    assert (rt.repo / CANONICAL_ONLY).read_text() == CANONICAL_BODY
    assert not (worktree / CANONICAL_ONLY).exists(), "canonical_only leaked into W"
    assert not (rt.repo / SESSION_ONLY).exists(), "session_only leaked into C"
    assert (worktree / SESSION_ONLY).read_text() == SESSION_BODY
    return worktree


# ══════════════════ SR-001 ══════════════════
async def test_sr001_canonical_read_sees_canonical_only(repo: Path) -> None:
    rt = Routing(repo)
    async with rt:
        await _two_target_setup(rt)
        envelope = await rt.tool("file.read", rt.args(CANONICAL, path=CANONICAL_ONLY))
        assert envelope["ok"] is True, envelope
        assert CANONICAL_BODY.strip() in rt.text_of(envelope)


async def test_sr001_canonical_read_cannot_see_session_only(repo: Path) -> None:
    """The substitution proof: this is the test SR-001 was failing."""

    rt = Routing(repo)
    async with rt:
        await _two_target_setup(rt)
        envelope = await rt.tool("file.read", rt.args(CANONICAL, path=SESSION_ONLY))
        assert envelope["ok"] is False, (
            "SR-001: canonical request was answered from the session worktree",
            envelope,
        )
        assert envelope.get("error_code") == "NOT_FOUND", envelope


async def test_sr001_session_read_cannot_see_canonical_only(repo: Path) -> None:
    rt = Routing(repo)
    async with rt:
        await _two_target_setup(rt)
        envelope = await rt.tool("file.read", rt.args(SESSION, path=CANONICAL_ONLY))
        assert envelope["ok"] is False, (
            "SR-001: session request was answered from the canonical checkout",
            envelope,
        )


async def test_sr001_session_read_sees_session_only(repo: Path) -> None:
    rt = Routing(repo)
    async with rt:
        await _two_target_setup(rt)
        envelope = await rt.tool("file.read", rt.args(SESSION, path=SESSION_ONLY))
        assert envelope["ok"] is True, envelope
        assert SESSION_BODY.strip() in rt.text_of(envelope)


async def test_sr001_canonical_search_cannot_see_session_only(repo: Path) -> None:
    rt = Routing(repo)
    async with rt:
        await _two_target_setup(rt)
        envelope = await rt.tool("file.search", rt.args(CANONICAL, query="SESSION-ONLY"))
        assert envelope["ok"] is False or "SESSION-ONLY" not in rt.text_of(envelope), (
            "SR-001: canonical search surfaced session-worktree content",
            envelope,
        )


async def test_sr001_canonical_git_status_runs_in_canonical(repo: Path) -> None:
    """§7 gateway-level positive: canonical git must report the canonical root."""

    rt = Routing(repo)
    async with rt:
        worktree = await _two_target_setup(rt)
        envelope = await rt.tool("git.status", rt.args(CANONICAL))
        assert envelope["ok"] is True, envelope
        result = envelope.get("result") or {}
        if result.get("cwd"):
            assert Path(result["cwd"]).resolve() == repo.resolve(), result
        if result.get("repo_root"):
            assert Path(result["repo_root"]).resolve() == repo.resolve(), result
        assert str(worktree) not in str(result.get("cwd") or ""), result


async def test_sr001_default_target_is_still_canonical(repo: Path) -> None:
    """§12: the default must not have been quietly changed."""

    from veya.remote.tool_adapter import resolve_execution_target

    assert resolve_execution_target(str(repo), intent="read") == CANONICAL
    assert resolve_execution_target(str(repo), intent="mutation") == "NEW_ISOLATED_WORKTREE"


# ══════════════════ SR-003 ══════════════════
def _assert_refused(envelope: dict[str, Any], label: str, worktree: Path) -> None:
    """§6: a refusal may never look like success against another tree."""

    assert envelope["ok"] is False, (label, envelope)
    result = envelope.get("result") or {}
    # The forbidden combination: dead cwd + canonical repo_root + ok.
    assert (
        not result.get("cwd") or Path(str(result["cwd"])).resolve() != repo_of(worktree).resolve()
    )
    assert result.get("repo_root") is None, (label, result)
    assert str(ENEMY_TEXT) not in str(result), (label, result)


ENEMY_TEXT = "SESSION-ONLY"


def repo_of(_worktree: Path) -> Path:
    # canonical root is three levels up from <root>/.veya/worktrees/<name>
    return _worktree.parents[2]


@pytest.mark.parametrize(
    "damage, label",
    [
        ("rmtree", "D1 missing worktree path"),
        ("empty_dir", "D2 empty directory"),
        ("delete_git", "D3 .git entry deleted"),
        ("dangling_gitdir", "D4 linked-worktree gitdir dangling"),
        ("metadata", "D5 worktree metadata missing"),
    ],
)
async def test_sr003_dead_worktree_matrix(repo: Path, damage: str, label: str) -> None:
    """D1-D5: every dead shape is refused, never answered from canonical."""

    rt = Routing(repo)
    async with rt:
        worktree = await _two_target_setup(rt)

        if damage == "rmtree":
            shutil.rmtree(worktree, ignore_errors=True)
        elif damage == "empty_dir":
            shutil.rmtree(worktree, ignore_errors=True)
            worktree.mkdir(parents=True, exist_ok=True)
        elif damage == "delete_git":
            (worktree / ".git").unlink()
        elif damage == "dangling_gitdir":
            (worktree / ".git").write_text("gitdir: /nonexistent/meta\n", encoding="utf-8")
        elif damage == "metadata":
            shutil.rmtree(worktree, ignore_errors=True)
            worktree.mkdir(parents=True, exist_ok=True)

        assert git(repo, "rev-parse", "--is-inside-work-tree") == "true"

        envelope = await rt.tool("file.read", rt.args(SESSION, path=SESSION_ONLY))
        _assert_refused(envelope, f"{label}/file.read", worktree)


async def test_sr003_d6_registered_target_invalid_checkout(repo: Path) -> None:
    """D6: checkout present, but the worktree's own metadata is gone.

    A linked worktree's real metadata lives in the canonical repo under
    .git/worktrees/<name>, referenced by the .git FILE. Removing that metadata
    leaves the files in place, so only a real worktree check catches it.
    """

    rt = Routing(repo)
    async with rt:
        worktree = await _two_target_setup(rt)

        pointer = (worktree / ".git").read_text(encoding="utf-8").strip()
        assert pointer.startswith("gitdir:"), pointer
        recorded = Path(pointer.split(":", 1)[1].strip())
        assert recorded.is_absolute()
        assert recorded.exists(), "linked-worktree metadata should exist"
        shutil.rmtree(recorded, ignore_errors=True)
        assert not recorded.exists()
        assert (worktree / SESSION_ONLY).exists(), "checkout files still present"

        envelope = await rt.tool("git.status", rt.args(SESSION))
        assert envelope["ok"] is False, envelope
        result = envelope.get("result") or {}
        assert result.get("repo_root") is None, result
        assert str(ENEMY_TEXT) not in str(result), envelope


async def test_sr003_cross_tool_refusal(repo: Path) -> None:
    """§5: read, search, git.status, git.diff and test.run all refuse."""

    rt = Routing(repo)
    async with rt:
        worktree = await _two_target_setup(rt)
        shutil.rmtree(worktree, ignore_errors=True)

        probes = [
            ("file.read", {"path": SESSION_ONLY}),
            ("file.search", {"query": ENEMY_TEXT}),
            ("git.status", {}),
            ("git.diff", {}),
            ("test.run", {"command": f'{sys.executable} -c "print(1)"', "wait": True}),
        ]
        for name, extra in probes:
            envelope = await rt.tool(name, rt.args(SESSION, **extra))
            assert envelope["ok"] is False, (name, envelope)
            result = envelope.get("result") or {}
            assert result.get("repo_root") is None, (name, result)
            assert ENEMY_TEXT not in str(result), (name, result)


async def test_sr003_git_mutations_still_refuse(repo: Path) -> None:
    """§5: the Git mutation surface must keep refusing a dead target."""

    rt = Routing(repo)
    async with rt:
        worktree = await _two_target_setup(rt)
        shutil.rmtree(worktree, ignore_errors=True)
        for name, extra in (
            ("git.stage", {"paths": ["calc.py"]}),
            ("git.commit", {"message": "m", "expect_paths": ["calc.py"]}),
            ("git.verify", {}),
            ("git.promote", {}),
        ):
            envelope = await rt.tool(name, rt.args(SESSION, **extra))
            assert envelope["ok"] is False, (name, envelope)
            assert (envelope.get("result") or {}).get("repo_root") is None, (name, envelope)


async def test_sr003_implicit_session_read_does_not_serve_canonical(repo: Path) -> None:
    """No explicit target, session worktree dead: must not answer from canonical."""

    rt = Routing(repo)
    async with rt:
        worktree = await _two_target_setup(rt)
        shutil.rmtree(worktree, ignore_errors=True)
        envelope = await rt.tool("file.read", {"workspace": str(repo), "path": SESSION_ONLY})
        assert envelope["ok"] is False, (
            "SR-003: dead session worktree silently served canonical content",
            envelope,
        )


async def test_sr003_canonical_still_works_while_session_is_dead(repo: Path) -> None:
    """A dead session worktree must not poison unrelated canonical work."""

    rt = Routing(repo)
    async with rt:
        worktree = await _two_target_setup(rt)
        shutil.rmtree(worktree, ignore_errors=True)
        envelope = await rt.tool("file.read", rt.args(CANONICAL, path=CANONICAL_ONLY))
        assert envelope["ok"] is True, envelope
        assert CANONICAL_BODY.strip() in rt.text_of(envelope)


# ══════════════════ unit-level seams ══════════════════
class _Binding:
    def __init__(self, repo_root: str) -> None:
        self.repo_root = repo_root
        self.requested_realpath = repo_root


class _Session:
    def __init__(self, worktrees: dict[str, str]) -> None:
        self.worktrees: ClassVar[dict[str, str]] = {}
        self.worktrees = worktrees


def test_sr001_base_dir_canonical_ignores_session_worktree(tmp_path: Path) -> None:
    from veya.remote.tool_adapter import RemoteToolAdapter

    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    session_wt = tmp_path / "wt"
    session_wt.mkdir()

    adapter = RemoteToolAdapter.__new__(RemoteToolAdapter)
    resolved = adapter._base_dir(
        _Session({str(repo): str(session_wt)}),  # type: ignore[arg-type]
        str(repo),
        execution_target=CANONICAL,
    )
    assert Path(resolved).resolve() == repo.resolve(), resolved


def test_sr003_base_dir_refuses_dead_session_worktree(tmp_path: Path) -> None:
    from veya.remote.tool_adapter import RemoteToolAdapter, RemoteToolAdapterError

    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    adapter = RemoteToolAdapter.__new__(RemoteToolAdapter)
    with pytest.raises(RemoteToolAdapterError) as excinfo:
        adapter._base_dir(
            _Session({str(repo): str(tmp_path / "gone")}),  # type: ignore[arg-type]
            str(repo),
            execution_target=SESSION,
        )
    assert excinfo.value.code == "POLICY_BLOCKED"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
