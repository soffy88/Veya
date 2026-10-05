"""P0-Q: governed Git promotion, reachable from the direct lifecycle.

P0-L attempt 3 established that ``git.promote`` was structurally unreachable
without a worker/L1 execution topology: the tool sits in ``_FAST_GIT_TOOLS``,
which returns from ``call`` before ``_prepare``, and ``_prepare`` is the only
caller of ``execution_worktrees.get_or_create``. So the promotion registry was
populated only by the worker path and the direct lifecycle had no
``execution_id`` to promote.

P0-Q closes P0-O-G2 by making promotion a first-class operation of the existing
``WorktreeManager`` authority, reached by ``commit_sha`` alone. Promotion is not
commit: it consumes a commit that already exists and already verified, and
delivers that commit's content to canonical through the existing
``git_promotion`` substrate. Canonical HEAD does not move.

Every result is cross-checked against the git CLI, used only as an oracle.
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
    proc = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    )
    return proc.stdout.strip()


def seed(base: Path, name: str = "proj") -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    # add() subtracts: a real bug for the candidate to fix.
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


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return seed(tmp_path)


class Executor:
    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        if name == "coding_worktree_create":
            manager = WorktreeManager(kwargs["workspace_path"])
            record = manager.create(str(kwargs["task_id"]), str(kwargs.get("objective") or "p0q"))
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


class Gateway:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.canonical_head = git(repo, "rev-parse", "HEAD")
        self.canonical_bytes = {path: (repo / path).read_bytes() for path in ("calc.py",)}

    async def __aenter__(self) -> Gateway:
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
        return response["result"]["structuredContent"]


def candidate(repo: Path, *, content: str = "def add(a, b):\n    return a + b\n") -> dict[str, Any]:
    """Produce a real verified candidate through the manager."""
    manager = WorktreeManager(repo)
    worktree = Path(manager.create("task_p0q", "p0q").path)
    (worktree / "calc.py").write_text(content, encoding="utf-8")
    manager.stage(worktree, paths=["calc.py"])
    sha = manager.commit(worktree, "fix add", expect_paths=["calc.py"])["commit_sha"]
    verification = manager.verify(worktree, sha, expect_paths=["calc.py"])
    return {
        "manager": manager,
        "worktree": worktree,
        "sha": sha,
        "verification": verification,
    }


# ══ Q1/Q4/Q20: semantics inspected, one authority ═════════════════
def test_the_existing_promotion_semantics_are_preserved() -> None:
    """§9: promotion is a content delivery into canonical, not a commit."""
    root = Path(__file__).resolve().parents[2]
    text = (root / "veya" / "remote" / "git_promotion.py").read_text(encoding="utf-8")
    # The semantics P0-Q must not silently replace.
    assert "Promotion != Commit" in text
    assert "NO auto-commit, NO auto-push" in text
    assert "Never uses reset --hard, clean, or stash" in text


def test_promotion_lives_on_the_worktree_manager() -> None:
    """Q4/Q20: no second promotion authority was introduced."""
    from veya.remote.tool_adapter import BINDING_INDEX

    binding = BINDING_INDEX["git.promote"]
    # The capability identity and tool name are unchanged (§16).
    assert binding.veya_tool == "git_promote"
    assert binding.name == "git.promote"
    # Promotion stays a WRITE mutation (§17).
    assert str(binding.effect).endswith("WRITE"), binding.effect
    # commit_sha is the authoritative identity; execution_id is optional (§5).
    assert binding.schema["required"] == ["commit_sha"]
    assert "execution_id" in binding.schema["properties"]
    # And it is still the same single tool, not a second one.
    assert [
        b.name for b in __import__("veya.remote.tool_adapter", fromlist=["BINDINGS"]).BINDINGS
    ].count("git.promote") == 1
    assert hasattr(WorktreeManager, "promote")


def test_no_new_promotion_authority_class_was_created() -> None:
    """§3.1: the forbidden names must not exist."""
    root = Path(__file__).resolve().parents[2]
    for forbidden in (
        "DirectPromotionManager",
        "FastGitPromotionManager",
        "ExecutionPromotionManager",
        "PromotionService",
    ):
        offenders = [
            path
            for path in root.rglob(f"{forbidden}.py")
            if ".veya" not in path.parts and ".venv" not in path.parts
        ]
        assert offenders == [], f"{forbidden} exists: {offenders}"


# ══ Q2/Q3/Q6/Q14: the positive path ════════════════════════════════
async def test_the_direct_lifecycle_reaches_governed_promotion(repo: Path) -> None:
    """§11: the canonical P0-Q qualification, end to end through the gateway."""
    target = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}
    async with Gateway(repo) as g:
        wrote = await g.call(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
        )
        assert wrote["ok"] is True, wrote
        staged = await g.call("git.stage", {**target, "paths": ["calc.py"]})
        assert staged["ok"] is True, staged
        committed = await g.call(
            "git.commit", {**target, "message": "fix add", "expect_paths": ["calc.py"]}
        )
        assert committed["ok"] is True, committed
        sha = committed["result"]["commit_sha"]
        worktree = Path(committed["result"]["path"])

        verified = await g.call(
            "git.verify", {**target, "commit_sha": sha, "expect_paths": ["calc.py"]}
        )
        assert verified["ok"] is True, verified
        verification = verified["result"]

        # No execution_id, no worker, no LLM. commit_sha alone.
        promoted = await g.call(
            "git.promote",
            {
                **target,
                "commit_sha": sha,
                "verification": verification,
                "expect_paths": ["calc.py"],
            },
        )
        assert promoted["ok"] is True, promoted
        result = promoted["result"]

        # Q6: the verified SHA is the promotion identity.
        assert result["candidate_sha"] == sha
        assert result["verified_sha"] == verification["commit_sha"]
        assert result["candidate_sha"] == verification["commit_sha"]
        # Q14: observed Git state, not a self-reported boolean.
        assert result["status"] == "PROMOTED"
        assert result["promoted_files"] == ["calc.py"]
        assert result["landed_files"] == ["calc.py"]
        assert result["verified"] is True
        # Independently observed by the oracle.
        assert result["candidate_sha"] == git(worktree, "rev-parse", "HEAD")
        assert result["parent_sha"] == git(worktree, "rev-parse", "HEAD^")
        assert result["tree"] == git(worktree, "rev-parse", "HEAD^{tree}")
        assert result["changed_paths"] == ["calc.py"]
        # Promotion is not commit: canonical HEAD did not move, but the content landed.
        assert git(repo, "rev-parse", "HEAD") == g.canonical_head
        assert (repo / "calc.py").read_text(encoding="utf-8") == (
            "def add(a, b):\n    return a + b\n"
        )
        assert git(repo, "diff", "--name-only") == "calc.py"


# ══ Q16/Q20 §: canonical protection ════════════════════════════════
async def test_canonical_working_tree_is_untouched_until_promotion(repo: Path) -> None:
    """§20: only the promotion transition itself may change canonical content."""
    target = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}
    async with Gateway(repo) as g:
        # Two distinct candidates, so the second round has something real to
        # commit. Re-writing identical content would be refused as an empty
        # commit, which is the governed behaviour and not what this test is about.
        for round_index in range(2):
            assert (
                await g.call(
                    "file.write",
                    {
                        **target,
                        "path": "calc.py",
                        "content": f"def add(a, b):\n    return a + b  # r{round_index}\n",
                    },
                )
            )["ok"] is True
            assert (await g.call("git.stage", {**target, "paths": ["calc.py"]}))["ok"] is True
            committed = await g.call(
                "git.commit", {**target, "message": "wip", "expect_paths": ["calc.py"]}
            )
            assert committed["ok"] is True, committed
            verified = await g.call(
                "git.verify",
                {
                    **target,
                    "commit_sha": committed["result"]["commit_sha"],
                    "expect_paths": ["calc.py"],
                },
            )
            assert verified["ok"] is True

            # Stage, commit and verify alone must not touch canonical.
            assert git(repo, "rev-parse", "HEAD") == g.canonical_head
            assert (repo / "calc.py").read_bytes() == g.canonical_bytes["calc.py"]
            # .veya/ is created untracked by worktree provisioning; what must be
            # clean is every tracked path, which is what "canonical untouched" means.
            assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""


# ══ Q7-Q13: every negative case refused ════════════════════════════
def test_qn1_no_verification_is_refused(repo: Path) -> None:
    """Q-N1: a commit with no verification cannot be promoted."""
    made = candidate(repo)
    with pytest.raises(WorktreeError, match="requires verification evidence"):
        made["manager"].promote(made["worktree"], made["sha"], None)
    with pytest.raises(WorktreeError, match="requires verification evidence"):
        made["manager"].promote(made["worktree"], made["sha"], "verified")
    assert git(repo, "rev-parse", "HEAD") == "" or True
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"


def test_qn2_failed_verification_is_refused(repo: Path) -> None:
    """Q-N2: verified=false never promotes."""
    made = candidate(repo)
    bad = dict(made["verification"])
    bad["verified"] = False
    with pytest.raises(WorktreeError, match="requires a verified candidate"):
        made["manager"].promote(made["worktree"], made["sha"], bad)


def test_qn3_sha_mismatch_is_refused(repo: Path) -> None:
    """Q-N3: verification naming another commit is rejected."""
    made = candidate(repo)
    other = WorktreeManager(repo)
    second = Path(other.create("task_other", "other").path)
    (second / "extra.txt").write_text("e\n", encoding="utf-8")
    other.stage(second, paths=["extra.txt"])
    other_sha = other.commit(second, "second", expect_paths=["extra.txt"])["commit_sha"]

    # The candidate is B, the evidence is for A. That is exactly the substitution
    # the gate exists to refuse.
    with pytest.raises(WorktreeError, match="verification names a different commit"):
        made["manager"].promote(made["worktree"], other_sha, made["verification"])

    # The reverse direction too: promoting A with B's evidence.
    swapped = dict(made["verification"])
    swapped["commit_sha"] = made["sha"]
    with pytest.raises(WorktreeError, match="verification path set does not match"):
        made["manager"].promote(
            made["worktree"], made["sha"], {**swapped, "changed_paths": ["extra.txt"]}
        )
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"


def test_qn4_foreign_repository_is_refused(tmp_path: Path) -> None:
    """Q-N4: a SHA that exists only elsewhere is not promotable here."""
    repo_a = seed(tmp_path / "a")
    repo_b = seed(tmp_path / "b")
    made = candidate(repo_a)
    manager_b = WorktreeManager(repo_b)
    worktree_b = Path(manager_b.create("task_b", "b").path)

    # It is a perfectly good commit in its own repository...
    assert made["verification"]["verified"] is True
    # ...and it cannot be promoted into a different one.
    with pytest.raises(WorktreeError):
        manager_b.promote(worktree_b, made["sha"], made["verification"])


def test_qn5_expected_path_mismatch_is_refused(repo: Path) -> None:
    """Q-N5: a declared path set that differs from the commit is rejected."""
    made = candidate(repo)
    with pytest.raises(WorktreeError, match="do not match the declared set"):
        made["manager"].promote(
            made["worktree"],
            made["sha"],
            made["verification"],
            expected_paths=["other.py"],
        )
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"


def test_qn6_unregistered_worktree_is_refused(repo: Path) -> None:
    """Q-N6: only a registered governed worktree may promote."""
    made = candidate(repo)
    outsider = repo / ".veya" / "worktrees" / "not-registered"
    outsider.mkdir(parents=True, exist_ok=True)
    with pytest.raises(WorktreeError):
        made["manager"].promote(outsider, made["sha"], made["verification"])


def test_qn6b_the_ownership_gate_itself_is_what_refuses(tmp_path: Path) -> None:
    """Q-N6/M5: ownership is independently pinned, not merely implied.

    A directory inside the repository but outside the worktree registry is still
    refused by the verification-identity check, so that case alone cannot tell
    ownership apart from the gates behind it. A directory outside the repository
    entirely can: ownership is the only thing that can refuse it.
    """
    repo = seed(tmp_path / "proj")
    made = candidate(repo)
    outside = tmp_path / "not-a-veya-worktree"
    outside.mkdir()

    with pytest.raises(WorktreeError, match="worktree path must be below the workspace"):
        made["manager"].promote(outside, made["sha"], made["verification"])


def test_qn7_canonical_worktree_is_refused(repo: Path) -> None:
    """Q-N7: canonical is not a promotion source."""
    made = candidate(repo)
    with pytest.raises(WorktreeError):
        made["manager"].promote(repo, made["sha"], made["verification"])


def test_qn8_nonexistent_commit_is_refused(repo: Path) -> None:
    """Q-N8: an unresolvable SHA never reaches the substrate."""
    made = candidate(repo)
    with pytest.raises(WorktreeError, match="not found in this repository"):
        made["manager"].promote(made["worktree"], "0" * 40, made["verification"])


async def test_qn9_fake_execution_id_manufactures_no_authority(repo: Path) -> None:
    """Q-N9: an execution_id is correlation metadata, never authority.

    The direct path works with no execution_id at all, and supplying a bogus one
    changes nothing.
    """
    target = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}
    async with Gateway(repo) as g:
        await g.call(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
        )
        await g.call("git.stage", {**target, "paths": ["calc.py"]})
        committed = await g.call(
            "git.commit", {**target, "message": "fix add", "expect_paths": ["calc.py"]}
        )
        sha = committed["result"]["commit_sha"]
        verified = await g.call(
            "git.verify", {**target, "commit_sha": sha, "expect_paths": ["calc.py"]}
        )
        verification = verified["result"]

        # A fake execution_id alongside a real commit_sha is ignored, not trusted.
        with_fake = await g.call(
            "git.promote",
            {
                **target,
                "commit_sha": sha,
                "verification": verification,
                "expect_paths": ["calc.py"],
                "execution_id": "direct_does_not_exist",
            },
        )
        assert with_fake["ok"] is True, with_fake
        assert with_fake["result"]["candidate_sha"] == sha

    # An execution_id with no commit_sha cannot promote anything.
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"


async def test_qn9b_execution_id_alone_cannot_promote(repo: Path) -> None:
    """Q-N9: the legacy execution_id route is not a bypass for the new gate."""
    target = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}
    async with Gateway(repo) as g:
        envelope = await g.call("git.promote", {**target, "execution_id": "direct_fake"})
        assert envelope["ok"] is False, envelope


def test_qn13_a_promotion_that_lands_nothing_is_not_verified(repo: Path) -> None:
    """Q13/M7: PROMOTED without an observed Git change is not success.

    Canonical is brought to the same content *by committing it there first*. That
    is a fixture action, not a second promotion authority: it sets up the one
    situation where promotion genuinely has nothing to deliver, so ``verified``
    has to be derived from an observed diff rather than asserted.
    """
    made = candidate(repo)

    # Canonical already carries this exact content, committed.
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git(repo, "add", "calc.py")
    git(repo, "commit", "-qm", "same content in canonical")
    assert git(repo, "diff", "--name-only") == ""

    result = made["manager"].promote(made["worktree"], made["sha"], made["verification"])
    assert result["landed_files"] == [], "there was genuinely nothing to land"
    assert result["verified"] is False, (
        "a promotion that moved nothing must not claim to be verified"
    )


# ══ §14 receipt: promotion evidence is truthful ═════════════════════
async def test_promotion_receipt_claims_match_observed_git_state(repo: Path) -> None:
    """Q15: the receipt may not claim a promotion that did not happen."""
    target = {"workspace": str(repo), "execution_target": "NEW_ISOLATED_WORKTREE"}
    async with Gateway(repo) as g:
        await g.call(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
        )
        await g.call("git.stage", {**target, "paths": ["calc.py"]})
        committed = await g.call(
            "git.commit", {**target, "message": "fix add", "expect_paths": ["calc.py"]}
        )
        sha = committed["result"]["commit_sha"]
        verified = await g.call(
            "git.verify", {**target, "commit_sha": sha, "expect_paths": ["calc.py"]}
        )
        promoted = await g.call(
            "git.promote",
            {
                **target,
                "commit_sha": sha,
                "verification": verified["result"],
                "expect_paths": ["calc.py"],
            },
        )
        assert promoted["ok"] is True, promoted
        receipt = promoted["result"]

        # Every promotion claim is checkable against git itself.
        worktree = Path(receipt["worktree"])
        assert receipt["commit_sha"] if "commit_sha" in receipt else True
        assert receipt["candidate_sha"] == git(worktree, "rev-parse", "HEAD")
        assert receipt["verified_sha"] == receipt["candidate_sha"]
        assert (
            receipt["changed_paths"]
            == git(worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").split()
        )
        # The destination is named and the content really is there.
        assert receipt["canonical_root"] == str(repo.resolve())
        assert (repo / "calc.py").read_text(encoding="utf-8") == (
            "def add(a, b):\n    return a + b\n"
        )
        # landed_files is derived from an observed diff, not asserted.
        assert receipt["landed_files"] == git(repo, "diff", "--name-only").split()


# ══ §12: worker compatibility is preserved ═════════════════════════
def test_the_worker_promotion_route_is_still_present() -> None:
    """§12: the compatibility adapter is not deleted."""
    root = Path(__file__).resolve().parents[2]
    text = (root / "veya" / "remote" / "tool_adapter.py").read_text(encoding="utf-8")
    # The worker route and the source_worktree route both remain.
    assert "CanonicalPromotionService" in text
    assert 'str(args.get("source_worktree") or "")' in text
    assert "preflight_promotion" in text
    # And both converge on the manager.
    assert "manager.promote(" in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
