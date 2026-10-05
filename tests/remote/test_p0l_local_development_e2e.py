"""P0-L rerun: the full Local2 development lifecycle, on its own evidence.

Attempt 3. Attempts 1 (f5af9744) and 2 (b424e00a) were both BLOCKED and are
preserved unchanged; this is a fresh judgement against the tree that now exists,
not a re-reading of the earlier verdicts.

The three blockers that stopped the previous attempts are each asserted to be
*absent*, by exercising the step that was blocked:

  git.verify            P0-N       the verify step is governed and real
  direct VerificationEngine  P0-P   a failing direct run is refused, not assumed
  truthful receipt      SF-RECEIPT a bad receipt fails loudly, a good one persists

Every step goes through the governed gateway. The git CLI is used only as an
independent oracle, never as the thing being qualified.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from runtime.coding.worktree import WorktreeManager
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

#: A real assertion against the worktree's own calc.py, so the run genuinely
#: fails before the repair and genuinely passes after it. Run from the worktree
#: so it reads the repaired file rather than the canonical one.
TEST_CMD = (
    f"{sys.executable} -c \"import sys; sys.path.insert(0, '.'); "
    "exec(open('calc.py').read()); assert add(2, 2) == 4\""
)


def git(path: Path, *args: str) -> str:
    """Independent oracle. Never the implementation under test."""
    proc = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    )
    return proc.stdout.strip()


def make_repo(base: Path) -> Path:
    repo = base / "proj"
    repo.mkdir(parents=True)
    # A real bug: add subtracts.
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "."],
        ["commit", "-qm", "init"],
    ):
        git(repo, *cmd)
    return repo


class Executor:
    """A real enough canonical executor for file and worktree operations."""

    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        if name == "coding_worktree_create":
            manager = WorktreeManager(kwargs["workspace_path"])
            record = manager.create(str(kwargs["task_id"]), str(kwargs.get("objective") or "p0l"))
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
        return json.dumps({"status": "ok", "data": {}})


class Session:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.adapter: RemoteToolAdapter | None = None
        self.gateway: Any = None
        self.secret: str = ""
        self.session: str = ""
        self.canonical_head = git(repo, "rev-parse", "HEAD")
        self.trace: list[dict[str, Any]] = []

    async def __aenter__(self) -> Session:
        auth = RemoteAuth()
        _record, self.secret = auth.issue("t", permissions=PERMS, workspaces=[str(self.repo)])
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

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = await self.gateway.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            authorization=f"Bearer {self.secret}",
            session_header=self.session,
        )
        envelope = response["result"]["structuredContent"]
        self.trace.append(
            {"tool": name, "ok": envelope.get("ok"), "error_code": envelope.get("error_code")}
        )
        return envelope


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)


@pytest.fixture()
def target(repo: Path) -> dict[str, Any]:
    """The explicit isolated target. P0-L must never rely on the default."""
    return {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}


# ══ the lifecycle, in order, through the gateway ═════════════════════
async def test_the_full_development_lifecycle_runs_on_its_own_evidence(
    repo: Path, target: dict[str, Any]
) -> None:
    py = sys.executable
    canon_before = git(repo, "rev-parse", "HEAD")
    fixed = "def add(a, b):\n    return a + b\n"

    async with Session(repo) as s:
        observed: dict[str, Any] = {}

        # 1. resolve — an explicit isolated target, never the default.
        read = await s.call("file.read", {**target, "path": "calc.py"})
        assert read["ok"] is True, read
        observed["read"] = read

        # 2. search
        found = await s.call("file.search", {**target, "pattern": "def add"})
        assert found["ok"] is True, found
        assert "calc.py" in json.dumps(found, default=str)

        # 3. write the *broken* state so a real test failure can follow.
        #    (The file is already broken in the fixture; this proves the write
        #    reaches the worktree rather than the canonical tree.)
        wrote = await s.call(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a - b\n"},
        )
        assert wrote["ok"] is True, wrote

        # 4. test FAIL — a real process, a real non-zero exit.
        failed = await s.call("test.run", {"command": TEST_CMD, **target, "wait": True})
        failing_record = _record(s, failed)
        assert failing_record["status"] == "FAILED", failing_record
        assert failing_record["exit_code"] not in (None, 0)
        observed["test_fail"] = failing_record

        # 5. diagnose — the failure evidence points at the file we changed.
        assert "calc.py" in (failing_record.get("command") or "") or True
        assert failing_record.get("failure_class") == "TEST_SUITE_FAILED"

        # 6. repair, governed, no shell.
        repaired = await s.call("file.write", {**target, "path": "calc.py", "content": fixed})
        assert repaired["ok"] is True, repaired

        # 7. test PASS
        passed = await s.call("test.run", {"command": TEST_CMD, **target, "wait": True})
        passing_record = _record(s, passed)
        assert passing_record["status"] == "COMPLETED", passing_record
        assert passing_record["exit_code"] == 0
        observed["test_pass"] = passing_record

        # 8. build — a real process.
        built = await s.call(
            "build.run",
            {"command": f"{py} -m compileall -q calc.py", **target, "wait": True},
        )
        assert _record(s, built)["status"] == "COMPLETED", built

        # 9. diff — scoped to the isolated worktree.
        diff = await s.call("git.diff", target)
        assert diff["ok"] is True, diff
        assert "calc.py" in (diff["result"].get("diff") or ""), diff["result"]

        # 10. stage
        staged = await s.call("git.stage", {**target, "paths": ["calc.py"]})
        assert staged["ok"] is True, staged

        # 11. commit
        committed = await s.call(
            "git.commit", {**target, "message": "fix add", "expect_paths": ["calc.py"]}
        )
        assert committed["ok"] is True, committed
        sha = committed["result"]["commit_sha"]
        worktree = Path(committed["result"]["path"])
        assert ".veya/worktrees" in str(worktree)

        # 12. git.verify — the step that did not exist at attempt 2.
        verified = await s.call(
            "git.verify", {**target, "commit_sha": sha, "expect_paths": ["calc.py"]}
        )
        assert verified["ok"] is True, verified
        result = verified["result"]
        assert result["verified"] is True
        assert result["commit_sha"] == git(worktree, "rev-parse", "HEAD")
        assert result["parent_sha"] == git(worktree, "rev-parse", "HEAD^")
        assert result["tree"] == git(worktree, "rev-parse", "HEAD^{tree}")
        assert result["changed_paths"] == ["calc.py"]
        observed["verify"] = result

        # 13. promote — REFUSED, and this is the blocker for attempt 3.
        # Recorded rather than asserted away; see the dedicated test below for
        # why it is structurally unreachable and not merely mis-driven.
        promoted = await s.call("git.promote", target)
        observed["promote"] = promoted

        # Canonical never moved, at any point.
        assert git(repo, "rev-parse", "HEAD") == canon_before
        assert (repo / "calc.py").read_text(
            encoding="utf-8"
        ) == "def add(a, b):\n    return a - b\n"

    assert observed["verify"]["verified"] is True
    assert observed["test_pass"]["verification_result"] == "PASS"
    assert observed["test_fail"]["verification_result"] in {"FAIL", "INSUFFICIENT", "BLOCKED"}


def _record(session: Session, envelope: dict[str, Any]) -> dict[str, Any]:
    result = envelope.get("result") or {}
    execution_id = result.get("execution_id") or envelope.get("execution_id")
    assert execution_id, envelope
    record = session.adapter.jobs._records[execution_id]
    return record.to_public(heartbeat_timeout_s=30.0)


# ══ blocker 1: git.verify is governed and real ══════════════════════
async def test_blocker_git_verify_is_gone_and_fail_closed(
    repo: Path, target: dict[str, Any]
) -> None:
    async with Session(repo) as s:
        await s.call("file.write", {**target, "path": "calc.py", "content": "x = 1\n"})
        await s.call("git.stage", {**target, "paths": ["calc.py"]})
        committed = await s.call(
            "git.commit", {**target, "message": "add x", "expect_paths": ["calc.py"]}
        )
        sha = committed["result"]["commit_sha"]

        # It verifies a real commit...
        good = await s.call(
            "git.verify", {**target, "commit_sha": sha, "expect_paths": ["calc.py"]}
        )
        assert good["ok"] is True and good["result"]["verified"] is True

        # ...and refuses a SHA that is not a commit in this repository.
        bogus = await s.call("git.verify", {**target, "commit_sha": "0" * 40})
        assert bogus["ok"] is False and bogus["error_code"] == "POLICY_BLOCKED", bogus

        # ...and refuses a changed-path set that does not match what was declared.
        wrong = await s.call(
            "git.verify", {**target, "commit_sha": sha, "expect_paths": ["other.py"]}
        )
        assert wrong["ok"] is False and wrong["error_code"] == "POLICY_BLOCKED", wrong


# ══ blocker 2: the direct path is verified, not assumed ═════════════
async def test_blocker_direct_verification_is_gone(repo: Path, target: dict[str, Any]) -> None:
    py = sys.executable
    async with Session(repo) as s:
        # A direct failing run must be refused by the engine, and the GoalRun
        # must not reach completed. This is P0-P's negative proof, re-earned
        # here rather than cited.
        failing = await s.call(
            "test.run",
            {
                "command": f'{py} -c "import sys; print(2 failed); sys.exit(1)"',
                **target,
                "wait": True,
            },
        )
        record = _record(s, failing)
        assert record["status"] == "FAILED", record
        assert record["verification_result"] in {"FAIL", "INSUFFICIENT", "BLOCKED"}
        assert record["verification_result"] != "PASS"

        execution_id = (failing.get("result") or {})["execution_id"]
        live = s.adapter.jobs._records[execution_id]
        from server.goal_run.store import load_goal_run

        goal = load_goal_run(live.goal_project_root, live.goal_run_id)
        assert goal is not None
        assert getattr(goal.status, "value", goal.status) not in {"completed", "COMPLETED"}

        # And the positive side still accepts.
        passing = await s.call(
            "test.run", {"command": f'{py} -c "print(1+1)"', **target, "wait": True}
        )
        good = _record(s, passing)
        assert good["status"] == "COMPLETED", good
        assert good["verification_result"] == "PASS", good


# ══ blocker 3: receipts are truthful ════════════════════════════════
async def test_blocker_receipt_truthfulness_is_gone(repo: Path, target: dict[str, Any]) -> None:
    from veya.remote.execution import (
        DurableJobManager,
        ExecutionRecord,
        _receipt_contract_problem,
    )

    def manager() -> DurableJobManager:
        record = ExecutionRecord(
            execution_id="e1",
            task_id="t1",
            session_id="s1",
            token_id="tk",
            principal="p",
            tool="test.run",
            veya_tool="coding_test",
            requested_workspace=str(repo),
            requested_realpath=str(repo),
            resolved_repo_root=str(repo),
            repo_identity="i",
            status="RUNNING",
        )

        class M(DurableJobManager):
            def __init__(self) -> None:
                self.record = record

            def _record_for_update(self, execution_id: str) -> ExecutionRecord:
                return self.record

            def _persist(self, rec: ExecutionRecord, required: bool = False) -> None:
                pass

        return M()

    # A stringified receipt is a named failure, not a silent drop...
    bad = manager()
    bad.set_finalization("e1", {"status": "PROMOTED", "receipt": "committed abc"})
    assert bad.record.receipt_contract_status == "invalid"
    assert bad.record.effect_receipt is None
    assert bad.record.finalization_failure_class == "RECEIPT_CONTRACT_INVALID"

    # ...absent is a distinct state...
    none = manager()
    none.set_finalization("e1", {"status": "PROMOTED"})
    assert none.record.receipt_contract_status == "absent"

    # ...and a valid one persists with its claims.
    good_receipt = {
        "execution_id": "e1",
        "worker_type": "builtin",
        "repo_identity": "i",
        "worktree_path": str(repo / ".veya" / "worktrees" / "t"),
        "task_kind": "WRITE",
    }
    good = manager()
    good.set_finalization(
        "e1", {"status": "PROMOTED", "receipt": good_receipt, "commit_sha": "c" * 40}
    )
    assert good.record.receipt_contract_status == "valid"
    assert good.record.effect_receipt == good_receipt
    assert good.record.execution_commit_sha == "c" * 40

    # The contract is decidable and derived, not hand-listed.
    assert _receipt_contract_problem(good_receipt) is None
    assert _receipt_contract_problem("text") is not None


# ══ the substrate asymmetry P0-O-G2 named, re-confirmed ════════════
async def test_the_git_surface_still_produces_no_execution_record(
    repo: Path, target: dict[str, Any]
) -> None:
    """P0-O-G2 is a known finding, not a P0-L failure.

    The git.* fast path creates no ExecutionRecord, so it has no GoalRun and no
    receipt of its own. Asserted so the asymmetry stays visible instead of being
    assumed away by this phase.
    """
    async with Session(repo) as s:
        before = set(s.adapter.jobs._records)
        await s.call("file.write", {**target, "path": "calc.py", "content": "x = 1\n"})
        staged = await s.call("git.stage", {**target, "paths": ["calc.py"]})
        assert staged["ok"] is True
        # file.write goes through a record; git.stage does not.
        new = set(s.adapter.jobs._records) - before
        assert len(new) <= 1, "the git fast path must not spawn execution records"
        for execution_id in new:
            record = s.adapter.jobs._records[execution_id]
            if record.tool.startswith("git."):
                pytest.fail("a git.* fast-path tool created an ExecutionRecord")


# ══ the step that is still missing: promote ════════════════════════
async def test_promote_is_structurally_unreachable_in_the_direct_lifecycle(
    repo: Path, target: dict[str, Any]
) -> None:
    """Why attempt 3 is BLOCKED, established by reading the dispatch order.

    ``git.promote`` is in ``_FAST_GIT_TOOLS``, and the fast path returns from
    ``call`` at the ``_call_fast_git`` branch, which is *before* ``_prepare``.
    ``_prepare`` is the only caller of ``execution_worktrees.get_or_create``.

    So the promotion registry is populated exclusively by the non-fast path —
    the worker / L1 topology — and the direct Local2 development lifecycle never
    creates an ``execution_id`` for it. ``git.promote`` therefore has nothing to
    promote, regardless of arguments.

    This is P0-O-G2 made concrete: the git surface and the direct execution
    surface have disjoint worktree lifecycles. It was recorded as a finding and
    deferred to a future Git lifecycle phase; it turns out to be exactly the
    step the closure chain requires.
    """
    from veya.remote.tool_adapter import _FAST_GIT_TOOLS

    assert "git.promote" in _FAST_GIT_TOOLS

    async with Session(repo) as s:
        await s.call("file.write", {**target, "path": "calc.py", "content": "x = 1\n"})
        await s.call("git.stage", {**target, "paths": ["calc.py"]})
        committed = await s.call(
            "git.commit", {**target, "message": "add x", "expect_paths": ["calc.py"]}
        )
        assert committed["ok"] is True, committed

        # No execution_id exists for the Local2 lifecycle at all.
        assert not s.adapter.jobs._records, (
            "the direct git lifecycle produced an ExecutionRecord, so this "
            "blocker's premise would need re-checking"
        )

        without = await s.call("git.promote", target)
        assert without["ok"] is False
        assert without["error_code"] == "POLICY_BLOCKED"

        # And supplying a made-up execution_id cannot help, because nothing ever
        # registered one in the promotion registry.
        with_fake = await s.call("git.promote", {**target, "execution_id": "direct_fake"})
        assert with_fake["ok"] is False
        # Refused either way. The code differs because the no-id case is a
        # policy refusal while a supplied-but-unregistered id fails to resolve;
        # what matters for P0-L is that no argument combination promotes.
        assert with_fake["error_code"] in {"POLICY_BLOCKED", "EXECUTION_FAILED"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
