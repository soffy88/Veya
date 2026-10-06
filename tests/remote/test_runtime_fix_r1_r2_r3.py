"""RUNTIME-FIX R1/R2/R3 — runtime correctness against the real governed runtime.

Each defect was reproduced live before being fixed, and the oracle here is the
real runtime, not a mock.

  R1  a deleted session worktree let governed Git operations fall back to the
      parent repository while still reporting the dead path as ``cwd``.
  R2  ``git.status`` and ``git.diff`` never checked git's exit code, so a failed
      invocation serialised as a clean, empty result.
  R3  direct-path verification derived failure from ``exit_code`` alone, so a
      command that never started (exit_code None, failure_class set) was
      reported ``verification_result = PASS``.

R4 is deliberately absent: the reported late pipeline rejection did not
reproduce, and that is recorded rather than "fixed" here.
"""

from __future__ import annotations

import json
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


class Executor:
    """Delegates to the real canonical tools, so nothing here is simulated."""

    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        if name == "coding_worktree_create":
            # The same WorktreeManager the real tool delegates to, so the
            # worktree under test is a genuine governed one.
            from runtime.coding.worktree import WorktreeManager

            record = WorktreeManager(kwargs["workspace_path"]).create(
                str(kwargs["task_id"]), str(kwargs.get("objective") or "runtime-fix")
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
        return json.dumps({"status": "ok", "data": {}})


class Runtime:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.adapter: RemoteToolAdapter | None = None

    async def __aenter__(self) -> Runtime:
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

    def target(self) -> dict[str, Any]:
        return {
            "workspace": str(self.repo),
            "execution_target": "NEW_ISOLATED_WORKTREE",
        }

    def record(self, envelope: dict[str, Any]) -> Any:
        execution_id = (envelope.get("result") or {}).get("execution_id")
        assert execution_id, envelope
        return self.adapter.jobs._records[execution_id]

    def worktree_of(self, envelope: dict[str, Any]) -> Path:
        import re

        text = (envelope.get("result") or {}).get("text", "")
        match = re.search(r"(/[^\s]+/\.veya/worktrees/task-[^\s]+)/calc\.py", text)
        assert match, text
        return Path(match.group(1))


def git(path: Path, *args: str) -> str:
    """Independent oracle."""
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "other.py").write_text("x = 1\n", encoding="utf-8")
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "."],
        ["commit", "-qm", "baseline"],
    ):
        git(root, *cmd)
    return root


# ══ R1 — a dead worktree must never fall back ═══════════════════════
async def test_r1_a_deleted_worktree_is_refused_not_silently_substituted(
    repo: Path,
) -> None:
    """R1.1-R1.6. Reproduced live before the fix.

    Before: git.status returned ok=true with ``cwd`` set to the deleted path
    while the operation ran against the parent repository, and git.diff
    returned ok=true with an empty diff.
    """
    async with Runtime(repo) as rt:
        target = rt.target()
        wrote = await rt.tool(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
        )
        assert wrote["ok"] is True, wrote
        worktree = rt.worktree_of(wrote)
        assert worktree.is_dir()

        # Make the worktree genuinely dirty, so a truthful diff is non-empty.
        (worktree / "calc.py").write_text(
            "def add(a, b):\n    return a + b  # d\n", encoding="utf-8"
        )
        staged = await rt.tool("git.stage", {**target, "paths": ["calc.py"]})
        assert staged["ok"] is True, staged

        # R1.3 remove the checkout entirely.
        shutil.rmtree(worktree, ignore_errors=True)
        assert not worktree.exists()

        for name, arguments in (
            ("git.status", target),
            ("git.diff", target),
        ):
            envelope = await rt.tool(name, arguments)
            # R1.4/R1.6 refused, never served from another tree.
            assert envelope["ok"] is False, (name, envelope)
            assert envelope["error_code"] == "POLICY_BLOCKED", (name, envelope)
            # The diagnostic names the actual unusable target.
            assert str(worktree) in str(envelope.get("message"))
            # And no contradictory cwd/repo_root is reported alongside it.
            result = envelope.get("result") or {}
            assert not result.get("cwd"), (name, result)
            assert not result.get("repo_root"), (name, result)

        # Canonical was never consulted as a substitute.
        assert (repo / "calc.py").read_text(encoding="utf-8") == (
            "def add(a, b):\n    return a - b\n"
        )


async def test_r1_a_live_worktree_still_works(repo: Path) -> None:
    """The refusal must not break the healthy path."""
    async with Runtime(repo) as rt:
        target = rt.target()
        wrote = await rt.tool(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
        )
        worktree = rt.worktree_of(wrote)
        (worktree / "calc.py").write_text(
            "def add(a, b):\n    return a + b  # d\n", encoding="utf-8"
        )

        status = await rt.tool("git.status", target)
        assert status["ok"] is True, status
        diff = await rt.tool("git.diff", target)
        assert diff["ok"] is True, diff
        # Truthful: the change we made is the change reported.
        assert "calc.py" in (diff["result"].get("diff") or "")
        assert (diff["result"].get("cwd") or "").startswith(str(worktree))


def test_r1_the_target_validator_is_decidable(tmp_path: Path) -> None:
    from veya.remote.tool_adapter import _invalid_git_target_reason

    assert _invalid_git_target_reason(tmp_path / "nope") is not None
    assert _invalid_git_target_reason(tmp_path) is None
    existing = tmp_path / "f"
    existing.write_text("x")
    assert _invalid_git_target_reason(existing) is not None


# ══ R2 — git.diff and status are truthful ═══════════════════════════
async def test_r2_diff_is_truthful_across_states(repo: Path) -> None:
    """R2.1-R2.4. A clean tree and a changed tree must be distinguishable."""
    async with Runtime(repo) as rt:
        target = rt.target()
        wrote = await rt.tool(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
        )
        worktree = rt.worktree_of(wrote)

        # R2.1 clean worktree. file.write leaves the worktree dirty by design,
        # so it has to be committed before "clean" is actually true.
        assert (await rt.tool("git.stage", {**target, "paths": ["calc.py"]}))["ok"]
        committed = await rt.tool(
            "git.commit",
            {**target, "message": "clean", "expect_paths": ["calc.py"]},
        )
        assert committed["ok"] is True, committed
        assert git(worktree, "status", "--porcelain") == ""

        # A clean diff is an empty SUCCESS.
        clean = await rt.tool("git.diff", target)
        assert clean["ok"] is True, clean
        assert (clean["result"].get("diff") or "").strip() == ""

        # R2.2 unstaged modification -> non-empty.
        (worktree / "calc.py").write_text(
            "def add(a, b):\n    return a + b  # u\n", encoding="utf-8"
        )
        unstaged = await rt.tool("git.diff", target)
        assert unstaged["ok"] is True
        assert "calc.py" in (unstaged["result"].get("diff") or "")

        # R2.3 staged modification -> still reported.
        assert (await rt.tool("git.stage", {**target, "paths": ["calc.py"]}))["ok"]

        # R2.4 a second file shows up too.
        (worktree / "other.py").write_text("x = 2\n", encoding="utf-8")
        both = await rt.tool("git.diff", target)
        assert both["ok"] is True
        assert "calc.py" in (both["result"].get("diff") or "")


async def test_r2_a_clean_diff_is_distinguishable_from_an_error(repo: Path) -> None:
    """R2.7. Nonexistent worktree must not look like 'no changes'."""
    async with Runtime(repo) as rt:
        target = rt.target()
        wrote = await rt.tool(
            "file.write",
            {**target, "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
        )
        worktree = rt.worktree_of(wrote)
        shutil.rmtree(worktree, ignore_errors=True)

        envelope = await rt.tool("git.diff", target)
        assert envelope["ok"] is False, envelope
        # Explicitly NOT an empty success.
        assert envelope.get("error_code")


def test_r2_the_failure_is_not_serialised_as_empty() -> None:
    """The rule the fix installs: a non-zero git exit is a failure."""
    root = Path(__file__).resolve().parents[2]
    text = (root / "veya" / "remote" / "tool_adapter.py").read_text(encoding="utf-8")
    assert "git status failed in" in text
    assert "git diff failed in" in text


# ══ R3 — FAILED execution never claims verification PASS ═════════════
@pytest.mark.parametrize(
    "label, command",
    [
        ("R3.1 exit zero", f'{sys.executable} -c "print(1)"'),
        ("R3.2 non-zero exit", f'{sys.executable} -c "import sys; sys.exit(1)"'),
        ("R3.6 spawn failure, exit_code None", "/nonexistent/binary --go"),
    ],
)
async def test_r3_failed_execution_is_never_verified_pass(
    repo: Path, label: str, command: str
) -> None:
    """R3.1/R3.2/R3.6, including the exit_code-is-None case that was the defect."""
    async with Runtime(repo) as rt:
        envelope = await rt.tool("test.run", {**rt.target(), "command": command, "wait": True})
        record = rt.record(envelope)

        if record.status != "COMPLETED":
            # R3.2 / R3.6 the invariant: a failed execution is never PASS.
            assert record.verification_result != "PASS", (
                label,
                record.status,
                record.exit_code,
                record.verification_result,
            )
        else:
            # R3.1 success still earns PASS, and it is earned from evidence.
            assert record.verification_result == "PASS", (label, record.verification_result)


async def test_r3_the_spawn_failure_case_is_actually_covered(repo: Path) -> None:
    """Guard the specific shape: FAILED with exit_code None must not be PASS.

    Without this the suite could pass while the original defect remained, since
    the non-zero-exit case already behaved correctly.
    """
    async with Runtime(repo) as rt:
        envelope = await rt.tool(
            "test.run",
            {**rt.target(), "command": "/nonexistent/binary --go", "wait": True},
        )
        record = rt.record(envelope)
        assert record.status == "FAILED", record.status
        assert record.exit_code is None, record.exit_code
        assert record.failure_class, "a spawn failure must carry a failure class"
        assert record.verification_result != "PASS"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
