"""SR-006 — the observation must describe the resolved target's repository.

Oracle: ``git rev-parse --show-toplevel`` executed *in the resolved target*.
A string comparison alone is not accepted.

Invariant under test:
    resolution.resolved_repo_root == actual git root of the resolved target
while ``repo_root`` keeps its distinct meaning of repository identity.

The decisive negative case (Do Not Re-Resolve): the resolved target differs from
the workspace binding, so an implementation that re-derives the repository from
the binding reports the canonical root while the command ran in a worktree.
"""

from __future__ import annotations

import re
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
CANONICAL = "CANONICAL_WORKTREE"
SESSION = "CURRENT_SESSION_WORKTREE"
SESSION_SENTINEL = "session_only_sr006.txt"


def git(path: Path | str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    ).stdout.strip()


class _Exec:
    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        if name == "coding_worktree_create":
            from runtime.coding.worktree import WorktreeManager

            record = WorktreeManager(kwargs["workspace_path"]).create(
                str(kwargs["task_id"]), str(kwargs.get("objective") or "sr006")
            )
            import json

            return json.dumps({"status": "ok", "data": {"worktree": record.to_dict()}}, default=str)
        if name == "write_file":
            import json

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
        return '{"status": "ok", "data": {}}'


class Rt:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    async def __aenter__(self) -> Rt:
        auth = RemoteAuth()
        _r, self.secret = auth.issue("sr006", permissions=PERMS, workspaces=[str(self.workspace)])
        audit = RemoteAudit(None)
        self.adapter = RemoteToolAdapter(
            _Exec(), redact=audit.redact, execution_store=ExecutionStore(None)
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
                "params": {"workspace": str(self.workspace)},
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
        return {"workspace": str(self.workspace), "execution_target": target, **extra}


def _make_repo(tmp_path: Path, name: str = "proj") -> Path:
    root = tmp_path / name
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


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path)


def _observed(result: dict[str, Any]) -> str:
    return str((result.get("resolution") or {}).get("resolved_repo_root") or "")


# ══ Case A — canonical: all four layers agree ════════════════════════
async def test_case_a_canonical_all_layers_agree(repo: Path) -> None:
    rt = Rt(repo)
    async with rt:
        envelope = await rt.tool("git.status", rt.args(CANONICAL))
        assert envelope["ok"] is True, envelope
        result = envelope["result"]
        oracle = git(repo, "rev-parse", "--show-toplevel")
        assert result["cwd"] == oracle
        assert _observed(result) == oracle, (result["cwd"], _observed(result), oracle)
        assert result["repo_root"] == oracle


# ══ Case B — session target: observation follows the target ═══════════
async def test_case_b_session_observation_names_the_session_repository(repo: Path) -> None:
    rt = Rt(repo)
    async with rt:
        planted = await rt.tool(
            "file.write",
            rt.args("NEW_ISOLATED_WORKTREE", path=SESSION_SENTINEL, content="W\n"),
        )
        assert planted["ok"] is True, planted
        match = re.search(
            r"(/[^\s]+/\.veya/worktrees/task-[^\s]+)/" + re.escape(SESSION_SENTINEL),
            (planted.get("result") or {}).get("text", ""),
        )
        assert match
        worktree = Path(match.group(1))
        assert not (repo / SESSION_SENTINEL).exists(), "sentinel must be worktree-only"

        envelope = await rt.tool("git.status", rt.args(SESSION))
        assert envelope["ok"] is True, envelope
        result = envelope["result"]
        oracle = git(worktree, "rev-parse", "--show-toplevel")
        assert result["cwd"] == oracle
        # THE SR-006 ASSERTION: the observation names the session repository,
        # not the canonical one the binding would have produced.
        assert _observed(result) == oracle, (result["cwd"], _observed(result), oracle)
        assert _observed(result) != str(repo.resolve())


# ══ Case C — nested repository selection is preserved ═════════════════
async def test_case_c_nested_repo_observation_preserves_selection(tmp_path: Path) -> None:
    parent = _make_repo(tmp_path, "parent")
    nested = parent / "stratum"
    nested.mkdir()
    (nested / "probe.py").write_text("print('nested')\n", encoding="utf-8")
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "."],
        ["commit", "-qm", "nested"],
    ):
        git(nested, *cmd)

    rt = Rt(parent)
    async with rt:
        envelope = await rt.tool(
            "git.status", {"workspace": str(parent), "workspace_path": "stratum"}
        )
        assert envelope["ok"] is True, envelope
        result = envelope["result"]
        oracle = git(nested, "rev-parse", "--show-toplevel")
        assert result["cwd"] == oracle
        assert _observed(result) == oracle, (result["cwd"], _observed(result), oracle)
        # The nested-selection intent must survive observation re-anchoring.
        assert (result.get("resolution") or {}).get("evidence", {}).get(
            "repo_discovery"
        ) == "nested_git_repository", result.get("resolution")


# ══ Do Not Re-Resolve — the decisive negative ════════════════════════
async def test_do_not_re_resolve_from_workspace_binding(repo: Path) -> None:
    """resolved target T != workspace binding C, and the report must follow T."""

    rt = Rt(repo)
    async with rt:
        await rt.tool(
            "file.write",
            rt.args("NEW_ISOLATED_WORKTREE", path=SESSION_SENTINEL, content="W\n"),
        )
        envelope = await rt.tool("git.status", rt.args(SESSION))
        result = envelope["result"]
        binding_root = str(repo.resolve())
        target = str(result["cwd"])
        assert target != binding_root, "precondition: target differs from binding"
        # An implementation that re-derives from the binding reports binding_root.
        assert _observed(result) != binding_root, (
            "SR-006 regressed: observation re-derived from the workspace binding",
            result,
        )


# ══ SR-005 preservation ══════════════════════════════════════════════
async def test_sr005_preserved_explicit_target_beats_execution_id(repo: Path) -> None:
    rt = Rt(repo)
    async with rt:
        await rt.tool(
            "file.write",
            rt.args("NEW_ISOLATED_WORKTREE", path=SESSION_SENTINEL, content="W\n"),
        )
        pinned = await rt.tool(
            "git.status",
            {**rt.args(CANONICAL), "execution_id": "exec-does-not-exist"},
        )
        result = pinned.get("result") or {}
        cwd = result.get("cwd")
        if pinned["ok"] and cwd:
            assert Path(cwd).resolve() == repo.resolve(), cwd


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
