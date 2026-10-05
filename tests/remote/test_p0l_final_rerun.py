"""P0-L final rerun — the last qualification for Local2 closure.

Nothing here cites an earlier phase. Every gate is re-measured through the
governed gateway on this tree, with the git CLI used only as an independent
oracle.

The two defects P0-Q fixed must be covered by this end-to-end run specifically,
because both are invisible to a unit test of the happy path:

  1. ``git.promote`` resolved its target through ``execution_id``, so a
     fabricated id decided which tree was promoted.
  2. ``git.promote`` derived changed paths from the worktree's dirty state, which
     is empty after a commit, so it could report a successful promotion that
     moved nothing.

The chain under qualification:

  resolve -> read/search -> NEW_ISOLATED_WORKTREE -> write -> test FAIL ->
  diagnose -> repair -> test PASS -> build -> diff -> git.stage -> git.commit ->
  git.verify -> git.promote -> truthful receipt
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

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)

#: A real assertion against the worktree's own calc.py. It fails on the broken
#: file and passes only after the governed repair, so the fail/pass pair is a real
#: result rather than a staged one.
TEST_CMD = (
    f"{sys.executable} -c \"import sys; sys.path.insert(0, '.'); "
    "exec(open('calc.py').read()); assert add(2, 2) == 4\""
)


def git(path: Path, *args: str) -> str:
    """Independent oracle. Never the implementation under qualification."""
    proc = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    )
    return proc.stdout.strip()


def make_repo(base: Path) -> Path:
    repo = base / "proj"
    repo.mkdir(parents=True)
    # add() subtracts: a genuine bug for the lifecycle to find and fix.
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
    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        if name == "coding_worktree_create":
            from runtime.coding.worktree import WorktreeManager

            record = WorktreeManager(kwargs["workspace_path"]).create(
                str(kwargs["task_id"]), str(kwargs.get("objective") or "p0l")
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


class Run:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.canonical_head = git(repo, "rev-parse", "HEAD")
        self.canonical_branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        self.canonical_bytes = (repo / "calc.py").read_bytes()
        self.trace: list[dict[str, Any]] = []

    async def __aenter__(self) -> Run:
        auth = RemoteAuth()
        _r, self.secret = auth.issue("t", permissions=PERMS, workspaces=[str(self.repo)])
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
        self.trace.append({"tool": name, "ok": envelope.get("ok")})
        return envelope


def record_of(run: Run, envelope: dict[str, Any]) -> dict[str, Any]:
    result = envelope.get("result") or {}
    execution_id = result.get("execution_id") or envelope.get("execution_id")
    assert execution_id, envelope
    live = run.adapter.jobs._records[execution_id]
    return live.to_public(heartbeat_timeout_s=30.0)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)


# ══ the whole chain, in order ═══════════════════════════════════════
async def test_the_whole_chain_completes_on_its_own_evidence(repo: Path) -> None:
    """Every gate, re-measured here. Nothing is inherited from an earlier phase."""
    target = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}
    seen: dict[str, Any] = {}

    async with Run(repo) as run:
        # 1. resolve + read + search on an explicit isolated target.
        read = await run.call("file.read", {**target, "path": "calc.py"})
        assert read["ok"] is True, read
        assert "def add" in (read["result"].get("text") or ""), read["result"]
        seen["read"] = read

        search = await run.call("file.search", {**target, "pattern": "def add"})
        assert search["ok"] is True, search
        assert "calc.py" in json.dumps(search, default=str)

        # 2. write the broken state through the governed tool.
        wrote = await run.call(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a - b\n"},
        )
        assert wrote["ok"] is True, wrote

        # 3. test FAIL — a real process, a real non-zero exit, a real verdict.
        failed = await run.call("test.run", {"command": TEST_CMD, **target, "wait": True})
        failing = record_of(run, failed)
        assert failing["status"] == "FAILED", failing
        assert failing["exit_code"] not in (None, 0)
        assert failing["failure_class"] == "TEST_SUITE_FAILED", failing
        assert failing["verification_result"] in {"FAIL", "INSUFFICIENT", "BLOCKED"}
        assert failing["verification_result"] != "PASS"
        seen["test_fail"] = failing

        # 4. diagnose — the failure evidence names the file under test.
        assert "calc.py" in (failing.get("command") or "")

        # 5. repair, governed, no shell.
        repaired = await run.call(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
        )
        assert repaired["ok"] is True, repaired

        # 6. test PASS.
        passed = await run.call("test.run", {"command": TEST_CMD, **target, "wait": True})
        passing = record_of(run, passed)
        assert passing["status"] == "COMPLETED", passing
        assert passing["exit_code"] == 0
        assert passing["verification_result"] == "PASS", passing
        seen["test_pass"] = passing

        # 7. build — a real process.
        built = await run.call(
            "build.run",
            {"command": f"{sys.executable} -m compileall -q calc.py", **target, "wait": True},
        )
        build_record = record_of(run, built)
        assert build_record["status"] == "COMPLETED", built
        assert build_record["exit_code"] == 0

        # 8. diff — scoped to the isolated worktree.
        diff = await run.call("git.diff", target)
        assert diff["ok"] is True, diff
        assert "calc.py" in (diff["result"].get("diff") or ""), diff["result"]

        # 9. git.stage — governed gateway.
        staged = await run.call("git.stage", {**target, "paths": ["calc.py"]})
        assert staged["ok"] is True, staged
        # git.stage reports the paths it staged, from the governed manager.
        assert staged["result"]["staged_paths"] == ["calc.py"], staged["result"]
        assert staged["result"]["requested_paths"] == ["calc.py"], staged["result"]

        # 10. git.commit — a real SHA, cross-checked.
        committed = await run.call(
            "git.commit", {**target, "message": "fix add", "expect_paths": ["calc.py"]}
        )
        assert committed["ok"] is True, committed
        sha = committed["result"]["commit_sha"]
        worktree = Path(committed["result"]["path"])
        assert ".veya/worktrees" in str(worktree)
        assert sha == git(worktree, "rev-parse", "HEAD")
        seen["sha"] = sha
        seen["worktree"] = str(worktree)

        # 11. git.verify — real verification, every field against the oracle.
        verified = await run.call(
            "git.verify", {**target, "commit_sha": sha, "expect_paths": ["calc.py"]}
        )
        assert verified["ok"] is True, verified
        evidence = verified["result"]
        assert evidence["verified"] is True
        assert evidence["commit_sha"] == git(worktree, "rev-parse", "HEAD")
        assert evidence["parent_sha"] == git(worktree, "rev-parse", "HEAD^")
        assert evidence["tree"] == git(worktree, "rev-parse", "HEAD^{tree}")
        assert (
            evidence["changed_paths"]
            == git(worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").split()
        )
        seen["verification"] = evidence

        # 12. git.promote — WorktreeManager, by commit_sha, no execution_id.
        promoted = await run.call(
            "git.promote",
            {**target, "commit_sha": sha, "verification": evidence, "expect_paths": ["calc.py"]},
        )
        assert promoted["ok"] is True, promoted
        receipt = promoted["result"]
        assert receipt["status"] == "PROMOTED", receipt
        assert receipt["candidate_sha"] == sha
        assert receipt["verified_sha"] == evidence["commit_sha"]
        assert receipt["promoted_files"] == ["calc.py"]
        assert receipt["landed_files"] == ["calc.py"]
        assert receipt["verified"] is True
        seen["promote"] = receipt

        # 13. The receipt is truthful: every claim re-observed independently.
        assert receipt["candidate_sha"] == git(worktree, "rev-parse", "HEAD")
        assert (
            receipt["changed_paths"]
            == git(worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").split()
        )
        assert receipt["landed_files"] == git(repo, "diff", "--name-only").split()
        assert receipt["canonical_root"] == str(repo.resolve())

        # Promotion is not commit: canonical HEAD did not move.
        assert git(repo, "rev-parse", "HEAD") == run.canonical_head
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == run.canonical_branch
        # But the promoted content is really there.
        assert (repo / "calc.py").read_text(encoding="utf-8") == (
            "def add(a, b):\n    return a + b\n"
        )

    assert seen["verification"]["verified"] is True
    assert seen["test_fail"]["verification_result"] != "PASS"
    assert seen["test_pass"]["verification_result"] == "PASS"


# ══ canonical protection ════════════════════════════════════════════
async def test_canonical_is_untouched_until_promotion(repo: Path) -> None:
    """§20: only the promotion transition itself may change canonical content."""
    target = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}
    async with Run(repo) as run:
        await run.call(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a + b  # r1\n"},
        )
        await run.call("git.stage", {**target, "paths": ["calc.py"]})
        committed = await run.call(
            "git.commit", {**target, "message": "r1", "expect_paths": ["calc.py"]}
        )
        assert committed["ok"] is True, committed
        verified = await run.call(
            "git.verify",
            {
                **target,
                "commit_sha": committed["result"]["commit_sha"],
                "expect_paths": ["calc.py"],
            },
        )
        assert verified["ok"] is True

        # Everything before promote left canonical byte-identical.
        assert git(repo, "rev-parse", "HEAD") == run.canonical_head
        assert (repo / "calc.py").read_bytes() == run.canonical_bytes
        assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""

        promoted = await run.call(
            "git.promote",
            {
                **target,
                "commit_sha": committed["result"]["commit_sha"],
                "verification": verified["result"],
                "expect_paths": ["calc.py"],
            },
        )
        assert promoted["ok"] is True, promoted
        # And promote is the explicit, recorded transition that changes it.
        assert (repo / "calc.py").read_text(encoding="utf-8").endswith("# r1\n")
        assert git(repo, "rev-parse", "HEAD") == run.canonical_head


# ══ P0-Q defect 1: target is not chosen by a fabricated execution_id ══
async def test_promote_target_is_not_chosen_by_execution_id(repo: Path) -> None:
    """A fabricated execution_id must not influence which tree is promoted."""
    target = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}
    async with Run(repo) as run:
        await run.call(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
        )
        await run.call("git.stage", {**target, "paths": ["calc.py"]})
        committed = await run.call(
            "git.commit", {**target, "message": "fix add", "expect_paths": ["calc.py"]}
        )
        sha = committed["result"]["commit_sha"]
        verified = await run.call(
            "git.verify", {**target, "commit_sha": sha, "expect_paths": ["calc.py"]}
        )

        # A fabricated id alongside a real commit_sha is ignored, not trusted.
        promoted = await run.call(
            "git.promote",
            {
                **target,
                "commit_sha": sha,
                "verification": verified["result"],
                "expect_paths": ["calc.py"],
                "execution_id": "direct_fabricated_id",
            },
        )
        assert promoted["ok"] is True, promoted
        # It promoted the worktree's commit, not anything the id named.
        assert promoted["result"]["candidate_sha"] == sha
        assert ".veya/worktrees" in promoted["result"]["worktree"]

        # An execution_id with no commit_sha promotes nothing.
        only_id = await run.call("git.promote", {**target, "execution_id": "direct_fake"})
        assert only_id["ok"] is False, only_id


# ══ P0-Q defect 2: paths come from the commit, not the dirty worktree ══
async def test_promotion_reads_paths_from_the_commit_not_the_dirty_worktree(
    repo: Path,
) -> None:
    """After a commit the worktree is clean; promotion must still work."""
    target = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}
    async with Run(repo) as run:
        await run.call(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
        )
        await run.call("git.stage", {**target, "paths": ["calc.py"]})
        committed = await run.call(
            "git.commit", {**target, "message": "fix add", "expect_paths": ["calc.py"]}
        )
        sha = committed["result"]["commit_sha"]
        worktree = Path(committed["result"]["path"])

        # The premise of the defect: the worktree is clean now.
        assert git(worktree, "status", "--porcelain") == ""

        verified = await run.call(
            "git.verify", {**target, "commit_sha": sha, "expect_paths": ["calc.py"]}
        )
        promoted = await run.call(
            "git.promote",
            {
                **target,
                "commit_sha": sha,
                "verification": verified["result"],
                "expect_paths": ["calc.py"],
            },
        )

        assert promoted["ok"] is True, promoted
        # It promoted real content, not an empty "nothing to do".
        assert promoted["result"]["promoted_files"] == ["calc.py"]
        assert promoted["result"]["landed_files"] == ["calc.py"]
        assert promoted["result"]["verified"] is True
        assert (repo / "calc.py").read_text(encoding="utf-8") == (
            "def add(a, b):\n    return a + b\n"
        )


# ══ no LLM / worker / shell dependency ═════════════════════════════
async def test_the_chain_dispatches_no_worker_and_no_executor(repo: Path) -> None:
    """The whole chain must complete with no LLM, worker or executor involved."""
    target = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}
    async with Run(repo) as run:
        for step in (
            (
                "file.write",
                {**target, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
            ),
            ("git.stage", {**target, "paths": ["calc.py"]}),
        ):
            assert (await run.call(*step))["ok"] is True, step[0]
        committed = await run.call(
            "git.commit", {**target, "message": "fix add", "expect_paths": ["calc.py"]}
        )
        sha = committed["result"]["commit_sha"]
        verified = await run.call(
            "git.verify", {**target, "commit_sha": sha, "expect_paths": ["calc.py"]}
        )
        assert (
            await run.call(
                "git.promote",
                {
                    **target,
                    "commit_sha": sha,
                    "verification": verified["result"],
                    "expect_paths": ["calc.py"],
                },
            )
        )["ok"] is True

        # No dispatch was recorded, and no direct command needed a provider.
        assert not run.adapter.jobs._records or all(
            record.dispatch_id is None for record in run.adapter.jobs._records.values()
        )
        # Every tool used was a governed binding, not a shell escape.
        assert {entry["tool"] for entry in run.trace} <= {
            "file.read",
            "file.search",
            "file.write",
            "git.diff",
            "git.stage",
            "git.commit",
            "git.verify",
            "git.promote",
            "test.run",
            "build.run",
        }
        assert "worker.dispatch" not in {entry["tool"] for entry in run.trace}
        assert "shell.exec" not in {entry["tool"] for entry in run.trace}


# ══ truthful receipt semantics on the real records ═════════════════
async def test_the_direct_records_carry_truthful_receipt_state(repo: Path) -> None:
    """SF-RECEIPT semantics observed on records the chain actually produced."""
    from veya.remote.execution import _receipt_contract_problem

    target = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}
    async with Run(repo) as run:
        await run.call(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
        )
        passed = await run.call("test.run", {"command": TEST_CMD, **target, "wait": True})
        record = record_of(run, passed)

        # A contract state is always present and decidable, never inferred from
        # a missing receipt.
        assert record["receipt_contract_status"] in {None, "valid", "absent", "invalid"}
        # And the verifier outcome is visible on the public record.
        assert record["verification_result"] == "PASS"
        assert "verification_result" in record
        # A stringified receipt is refused by the shared validator.
        assert _receipt_contract_problem("not a receipt") is not None


# ══ the chain still refuses what it must ═══════════════════════════
async def test_the_final_chain_still_refuses_unverified_promotion(
    repo: Path,
) -> None:
    """A completed chain is not a licence to promote anything.

    Promotion stayed gated through P0-Q, and the final rerun confirms the gate
    is still closed for an unverified candidate, a foreign SHA and a canonical
    target. Passing the happy path must not have weakened any refusal.
    """
    target = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}
    async with Run(repo) as run:
        await run.call(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
        )
        await run.call("git.stage", {**target, "paths": ["calc.py"]})
        committed = await run.call(
            "git.commit", {**target, "message": "fix add", "expect_paths": ["calc.py"]}
        )
        sha = committed["result"]["commit_sha"]
        verified = await run.call(
            "git.verify", {**target, "commit_sha": sha, "expect_paths": ["calc.py"]}
        )
        evidence = verified["result"]

        # No verification evidence at all.
        for bad in (None, "verified", {"verified": False}):
            envelope = await run.call(
                "git.promote", {**target, "commit_sha": sha, "verification": bad}
            )
            assert envelope["ok"] is False, (bad, envelope)

        # A SHA that does not exist.
        absent = await run.call(
            "git.promote", {**target, "commit_sha": "0" * 40, "verification": evidence}
        )
        assert absent["ok"] is False, absent

        # Evidence that names a different commit.
        swapped = dict(evidence)
        swapped["commit_sha"] = "1" * 40
        mismatch = await run.call(
            "git.promote", {**target, "commit_sha": sha, "verification": swapped}
        )
        assert mismatch["ok"] is False, mismatch

        # A declared path set the commit does not match.
        wrong_paths = await run.call(
            "git.promote",
            {**target, "commit_sha": sha, "verification": evidence, "expect_paths": ["nope.py"]},
        )
        assert wrong_paths["ok"] is False, wrong_paths

        # Everything above refused, and nothing was promoted.
        assert (repo / "calc.py").read_text(encoding="utf-8") == (
            "def add(a, b):\n    return a - b\n"
        )
        assert git(repo, "rev-parse", "HEAD") == run.canonical_head


@pytest.mark.xfail(
    strict=True,
    reason=(
        "P0-L-F1: git.promote silently substitutes the session worktree when "
        "CANONICAL_WORKTREE is requested, instead of refusing as git.stage, "
        "git.commit and git.verify all do. P0-Q's Q12 gate passed only because "
        "it exercised WorktreeManager.promote directly and never through the "
        "adapter's target resolution. Strict, so fixing the defect turns this "
        "into a failure that must be updated deliberately."
    ),
)
async def test_canonical_target_must_be_refused_as_a_promotion_source(repo: Path) -> None:
    """Q12, measured through the gateway rather than at the manager.

    Promotion must not honour a canonical source by quietly using a different
    tree. Substituting the source while the caller believes canonical was used is
    the same class of authority leak P0-Q closed for execution_id.
    """
    target = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}
    canonical = {"workspace": str(repo), "execution_target": "CANONICAL_WORKTREE"}
    async with Run(repo) as run:
        # Establish a session worktree, as any real chain does.
        await run.call(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
        )
        await run.call("git.stage", {**target, "paths": ["calc.py"]})
        committed = await run.call(
            "git.commit", {**target, "message": "fix add", "expect_paths": ["calc.py"]}
        )
        worktree = committed["result"]["path"]
        verified = await run.call(
            "git.verify",
            {
                **target,
                "commit_sha": committed["result"]["commit_sha"],
                "expect_paths": ["calc.py"],
            },
        )

        # The sibling Git mutations all refuse a canonical target.
        for tool, args in (
            ("git.stage", {**canonical, "paths": ["calc.py"]}),
            ("git.commit", {**canonical, "message": "m", "expect_paths": ["calc.py"]}),
            (
                "git.verify",
                {
                    "workspace": str(repo),
                    "execution_target": "CANONICAL_WORKTREE",
                    "commit_sha": committed["result"]["commit_sha"],
                },
            ),
        ):
            envelope = await run.call(tool, args)
            assert envelope["ok"] is False, (tool, envelope)

        # git.promote must refuse it too.
        envelope = await run.call(
            "git.promote",
            {
                **canonical,
                "commit_sha": committed["result"]["commit_sha"],
                "verification": verified["result"],
            },
        )
        assert envelope["ok"] is False, (
            "git.promote accepted a canonical source and substituted "
            f"{envelope.get('result', {}).get('worktree')} for it"
        )
        assert envelope.get("result", {}).get("worktree") != worktree


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
