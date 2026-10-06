"""SESSION-WORKTREE ROUTING CORRECTNESS - R1..R10 against the real runtime.

.. WARNING::
   THIS SUITE FAILS AGAINST BASELINE 412e855b BY DESIGN.

   It is the reproduction artifact for SR-001..SR-004 (session-worktree routing
   drift). It is deliberately NOT registered in ``scripts/ci_test_suites.py``
   so it cannot break the green baseline while the fix is pending.

   Against 412e855b: 8 of 13 failed. The failures are the defect, not a broken
   test. Do not "fix" them by relaxing assertions -- see
   ``local2_session_routing_findings.md`` for the root causes.

Baseline: 412e855b.

The decisive oracle used throughout is a file that exists **only inside the
session worktree** (``W``) and **not** in the canonical checkout (``C``):

* ``file.read`` of that file with ``CANONICAL_WORKTREE`` must FAIL.
  If it succeeds, the canonical request was silently routed into ``W``.
* ``file.read`` of that file with ``CURRENT_SESSION_WORKTREE`` must SUCCEED.
  If it fails, session routing is broken.

That single asymmetry proves or disproves routing without touching private
state. Git-side checks compare the tool's reported cwd/repo_root against
independent ``git rev-parse`` oracles.

No mocked git, no modified expectations, no worktree deletion outside tmp.
"""

from __future__ import annotations

import json
import os
import shutil
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

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)

# The sentinel that exists only in the session worktree.
MARKER = "SESSION_ONLY_MARKER = 1\n"


def git(path: Path | str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    ).stdout.strip()


class Executor:
    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        if name == "coding_worktree_create":
            from runtime.coding.worktree import WorktreeManager

            record = WorktreeManager(kwargs["workspace_path"]).create(
                str(kwargs["task_id"]), str(kwargs.get("objective") or "routing")
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
                                str(kwargs.get("filepath")),
                                str(kwargs.get("content", "")),
                                True,
                            )
                        )
                    },
                },
                default=str,
            )
        if name == "read_file":
            from server.tool_registry import _tool_read_file

            return json.dumps(
                {
                    "status": "ok",
                    "data": {"result": str(_tool_read_file(str(kwargs.get("filepath"))))},
                },
                default=str,
            )
        return json.dumps({"status": "ok", "data": {}})


class Routing:
    """Real gateway + real WorktreeManager, no internals poked."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    async def __aenter__(self) -> Routing:
        auth = RemoteAuth()
        _r, self.secret = auth.issue("routing", permissions=PERMS, workspaces=[str(self.repo)])
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


async def _make_session_worktree(rt: Routing) -> Path:
    """Create W through the governed path and plant the W-only marker."""

    envelope = await rt.tool(
        "file.write",
        rt.args("NEW_ISOLATED_WORKTREE", path="calc.py", content=MARKER),
    )
    assert envelope["ok"] is True, envelope
    import re

    text = (envelope.get("result") or {}).get("text", "")
    match = re.search(r"(/[^\s]+/\.veya/worktrees/task-[^\s]+)/calc\.py", text)
    assert match, text
    worktree = Path(match.group(1))
    assert worktree.is_dir()
    assert not (rt.repo / "calc.py").read_text().startswith("SESSION_ONLY")
    return worktree


# ══ R1 — explicit CANONICAL_WORKTREE never substitutes the session tree ═
async def test_r1_canonical_read_is_not_served_from_session_worktree(repo: Path) -> None:
    """A file that exists ONLY in the session worktree must be invisible to a
    canonical read. calc.py exists in both trees, so it cannot prove routing;
    session_only.txt exists in W alone and therefore can."""

    rt = Routing(repo)
    async with rt:
        await _make_session_worktree(rt)
        planted = await rt.tool(
            "file.write",
            rt.args("NEW_ISOLATED_WORKTREE", path="session_only.txt", content="W ONLY\n"),
        )
        assert planted["ok"] is True, planted
        assert not (repo / "session_only.txt").exists()

        blocked = await rt.tool("file.read", rt.args("CANONICAL_WORKTREE", path="session_only.txt"))
        assert blocked["ok"] is False, (
            "R1 DRIFT: canonical read returned the session worktree's file",
            blocked,
        )


async def test_r1_canonical_read_returns_canonical_content(repo: Path) -> None:
    rt = Routing(repo)
    async with rt:
        await _make_session_worktree(rt)
        # calc.py exists in C with different content; canonical read must be C's.
        envelope = await rt.tool("file.read", rt.args("CANONICAL_WORKTREE", path="calc.py"))
        if envelope["ok"]:
            text = str((envelope.get("result") or {}).get("text", ""))
            assert "SESSION_ONLY_MARKER" not in text, "R1 DRIFT: served W content"
            assert "def add" in text


async def test_r1_canonical_git_mutations_stay_blocked(repo: Path) -> None:
    """Canonical mutation policy must remain in force even with a session tree."""

    rt = Routing(repo)
    async with rt:
        await _make_session_worktree(rt)
        for name, extra in (
            ("git.stage", {"paths": ["calc.py"]}),
            ("git.commit", {"message": "m", "expect_paths": ["calc.py"]}),
            ("git.verify", {}),
            ("git.promote", {}),
        ):
            envelope = await rt.tool(name, rt.args("CANONICAL_WORKTREE", **extra))
            assert envelope["ok"] is False, (name, envelope)
            assert envelope.get("error_code") == "POLICY_BLOCKED", (name, envelope)


# ══ R2 — explicit CURRENT_SESSION_WORKTREE ════════════════════════════
async def test_r2_current_session_worktree_target_is_supported(repo: Path) -> None:
    rt = Routing(repo)
    async with rt:
        await _make_session_worktree(rt)
        envelope = await rt.tool("file.read", rt.args("CURRENT_SESSION_WORKTREE", path="calc.py"))
        assert envelope["ok"] is True, (
            "R2 BROKEN: CURRENT_SESSION_WORKTREE is not an accepted target",
            envelope,
        )
        assert "SESSION_ONLY_MARKER" in str((envelope.get("result") or {}).get("text", ""))


async def test_r2_session_mutation_lands_only_in_session_worktree(repo: Path) -> None:
    rt = Routing(repo)
    async with rt:
        worktree = await _make_session_worktree(rt)
        before = git(repo, "rev-parse", "HEAD")

        await rt.tool(
            "file.write",
            rt.args("CURRENT_SESSION_WORKTREE", path="calc.py", content="X = 2\n"),
        )
        assert (worktree / "calc.py").read_text() == "X = 2\n"
        # Canonical is byte-identical and its HEAD never moved.
        assert (repo / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
        assert git(repo, "rev-parse", "HEAD") == before


# ══ R3 — dead worktree never falls back ═══════════════════════════════
async def test_r3_dead_session_worktree_never_falls_back_to_canonical(repo: Path) -> None:
    rt = Routing(repo)
    async with rt:
        worktree = await _make_session_worktree(rt)
        shutil.rmtree(worktree, ignore_errors=True)
        assert not worktree.exists()

        for name, extra in (
            ("file.read", {"path": "calc.py"}),
            ("file.search", {"query": "MARKER"}),
            ("git.status", {}),
            ("git.diff", {}),
        ):
            envelope = await rt.tool(name, rt.args("CURRENT_SESSION_WORKTREE", **extra))
            assert envelope["ok"] is False, (name, envelope)
            # Must not hand back canonical content.
            assert "SESSION_ONLY_MARKER" not in str(envelope), (name, envelope)


# ══ R4 — missing / empty worktree ═════════════════════════════════════
async def test_r4_empty_directory_is_not_a_valid_checkout(repo: Path) -> None:
    """A directory that exists but holds no Git checkout must not resolve upward."""

    rt = Routing(repo)
    async with rt:
        empty = repo / ".veya" / "worktrees" / "task-empty"
        empty.mkdir(parents=True, exist_ok=True)

        # git.* do not take a path target -- the workspace IS the target. Asking
        # for a non-checkout workspace must be refused, not resolved upward.
        envelope = await rt.tool(
            "git.status", {"workspace": str(empty), "execution_target": "CANONICAL_WORKTREE"}
        )
        assert envelope["ok"] is False, envelope
        assert envelope.get("error_code"), envelope


def test_r4_target_validator_rejects_non_checkout(tmp_path: Path) -> None:
    from veya.remote.tool_adapter import _invalid_git_target_reason

    empty = tmp_path / "empty"
    empty.mkdir()
    reason = _invalid_git_target_reason(empty)
    assert reason is not None, "an empty directory must not pass as a git target"


# ══ R5 — foreign / unregistered worktree ══════════════════════════════
async def test_r5_foreign_repository_is_blocked(tmp_path: Path, repo: Path) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "calc.py").write_text("SECRET = 'foreign'\n", encoding="utf-8")
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "f@f"],
        ["config", "user.name", "f"],
        ["add", "."],
        ["commit", "-qm", "foreign"],
    ):
        git(foreign, *cmd)

    rt = Routing(repo)
    async with rt:
        envelope = await rt.tool("git.status", rt.args("CANONICAL_WORKTREE", path=str(foreign)))
        assert envelope["ok"] is False, envelope
        assert envelope.get("error_code") in {"POLICY_BLOCKED", "WORKSPACE_DENIED"}, envelope
        # No mutation escaped into the foreign repo.
        assert git(foreign, "status", "--porcelain") == ""


# ══ R7 — git.diff three-state truthfulness ═══════════════════════════
async def test_r7_diff_three_states_never_conflate(repo: Path) -> None:
    rt = Routing(repo)
    async with rt:
        worktree = await _make_session_worktree(rt)
        args = rt.args("CURRENT_SESSION_WORKTREE")

        # NO_DIFF: clean tree is an empty SUCCESS.
        assert (await rt.tool("git.stage", {**args, "paths": ["calc.py"]}))["ok"]
        assert (await rt.tool("git.commit", {**args, "message": "w", "expect_paths": ["calc.py"]}))[
            "ok"
        ]
        clean = await rt.tool("git.diff", args)
        assert clean["ok"] is True
        assert (clean["result"].get("diff") or "").strip() == ""

        # HAS_DIFF: matches the independent oracle.
        (worktree / "calc.py").write_text("Y = 3\n", encoding="utf-8")
        dirty = await rt.tool("git.diff", args)
        assert dirty["ok"] is True
        oracle = subprocess.run(
            ["git", "-C", str(worktree), "diff"], capture_output=True, text=True, check=False
        ).stdout
        reported = dirty["result"].get("diff") or ""
        assert "calc.py" in reported
        assert reported.strip() == oracle.strip(), "diff must match the git oracle"


async def test_r7_dead_worktree_diff_is_not_clean(repo: Path) -> None:
    rt = Routing(repo)
    async with rt:
        worktree = await _make_session_worktree(rt)
        shutil.rmtree(worktree, ignore_errors=True)
        envelope = await rt.tool("git.diff", rt.args("CURRENT_SESSION_WORKTREE"))
        assert envelope["ok"] is False, envelope
        assert envelope.get("error_code")


# ══ R8 — response truthfulness against os.getcwd / git rev-parse ══════
async def test_r8_reported_target_matches_reality(repo: Path) -> None:
    rt = Routing(repo)
    async with rt:
        worktree = await _make_session_worktree(rt)
        for target, expect in (
            ("CURRENT_SESSION_WORKTREE", worktree),
            ("CANONICAL_WORKTREE", repo),
        ):
            envelope = await rt.tool("git.status", rt.args(target))
            if not envelope["ok"]:
                continue
            result = envelope.get("result") or {}
            cwd = result.get("cwd")
            if cwd:
                assert Path(cwd).resolve() == expect.resolve(), (target, cwd, expect)
                assert Path(cwd).resolve() == Path(os.path.realpath(cwd))
            # Two distinct claims, two distinct fields:
            #   repo_root                    -> repository IDENTITY (canonical)
            #   resolution.resolved_repo_root -> the tree actually used
            # The observation field is the one that must match git; asserting the
            # identity field against the oracle was a wrong field mapping.
            observed_root = (result.get("resolution") or {}).get("resolved_repo_root")
            if observed_root:
                assert (
                    Path(observed_root).resolve()
                    == Path(git(expect, "rev-parse", "--show-toplevel")).resolve()
                ), (target, observed_root)
            identity_root = result.get("repo_root")
            if identity_root:
                assert Path(identity_root).resolve() == repo.resolve(), (
                    target, identity_root, repo,
                )


# ══ R9 — session lifecycle must not silently drop a dead worktree ══════
async def test_r9_dead_worktree_is_not_silently_discarded(repo: Path) -> None:
    """_base_dir used to pop a dead registration and fall back to canonical."""

    rt = Routing(repo)
    async with rt:
        worktree = await _make_session_worktree(rt)
        shutil.rmtree(worktree, ignore_errors=True)
        # No target specified: the session still believes in W. That must not
        # silently become "canonical" for a session-bound request.
        envelope = await rt.tool("file.read", {"workspace": str(repo), "path": "calc.py"})
        assert "SESSION_ONLY_MARKER" not in str(envelope), (
            "R9 DRIFT: dead session worktree silently served canonical content"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ══ R8b — execution_id must not override an explicit target (M8) ══════
async def test_r8b_execution_id_cannot_override_explicit_target(repo: Path) -> None:
    """A stale/fabricated execution_id must not relocate a pinned request.

    P0-Q established that git.promote resolves through the governed seam; this
    pins the broader rule that an explicit execution_target always wins.
    """

    rt = Routing(repo)
    async with rt:
        worktree = await _make_session_worktree(rt)

        # Canonical pinned, but an execution_id is also supplied.
        envelope = await rt.tool(
            "git.status",
            {
                **rt.args("CANONICAL_WORKTREE"),
                "execution_id": "exec-does-not-exist",
            },
        )
        result = envelope.get("result") or {}
        cwd = result.get("cwd")
        if envelope["ok"] and cwd:
            assert Path(cwd).resolve() == repo.resolve(), (
                "M8: execution_id relocated an explicitly canonical request",
                cwd,
            )
        assert str(worktree) not in str(cwd or ""), cwd
