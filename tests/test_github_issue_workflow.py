from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from runtime.github_issue import (
    GitHubIssueContext,
    fetch_issue_context,
    prepare_issue_fix,
    publish_issue_draft,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _repo(tmp_path: Path, *, failing_tests: bool = False) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Issue Workflow Tests")
    (root / "app.py").write_text("print('base')\n", encoding="utf-8")
    test_command = (
        f'{sys.executable} -c "raise SystemExit(1)"'
        if failing_tests
        else f"{sys.executable} -c \"print('tests pass')\""
    )
    (root / "AGENTS.md").write_text(
        "## TEST\n"
        f"- test: {test_command}\n"
        f"- lint: {sys.executable} -c \"print('lint pass')\"\n"
        f"- typecheck: {sys.executable} -c \"print('types pass')\"\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _patch() -> str:
    return """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-print('base')
+print('fixed')
"""


def _fake_github(monkeypatch: pytest.MonkeyPatch, *, updated_at: str = "2026-08-30T00:00:00Z"):
    from runtime import github_issue

    def fake_json(args: list[str]) -> object:
        if args[:2] == ["issue", "view"]:
            return {
                "number": 42,
                "title": "Fix app output",
                "body": "The output is wrong; see `app.py`.",
                "labels": [{"name": "bug"}],
                "comments": [{"body": "Please keep the change small."}],
                "updatedAt": updated_at,
            }
        if args[:2] == ["repo", "view"]:
            return {"defaultBranchRef": {"name": "main"}}
        if "timeline" in " ".join(args):
            return []
        raise AssertionError(f"unexpected GitHub query: {args}")

    monkeypatch.setattr(github_issue, "_gh_json", fake_json)


@pytest.mark.asyncio
async def test_issue_fetch_and_verified_patch_dogfood(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, base_sha = _repo(tmp_path)
    _fake_github(monkeypatch)

    context = fetch_issue_context("acme/demo", 42, workspace_path=root)
    assert context.labels == ["bug"]
    assert context.referenced_files == ["app.py"]
    assert context.base_sha == base_sha

    result = await prepare_issue_fix(
        "acme/demo",
        42,
        _patch(),
        workspace_path=root,
        profile="local_trusted",
    )
    assert result["status"] == "ok"
    data = result["data"]
    assert data["final_result"]["status"] == "completed"
    assert data["final_result"]["pr_ready"] is True
    assert data["pr_draft"]["status"] == "PR_READY"
    output = root / ".veya" / "runs" / data["task_id"] / "outputs"
    assert {
        "issue_context.json",
        "diff.patch",
        "verification_report.json",
        "artifact_manifest.json",
        "pr_draft.json",
        "final_result.json",
    } <= {path.name for path in output.iterdir()}
    assert "print('fixed')" in (output / "diff.patch").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_failed_required_sensor_never_becomes_pr_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _repo(tmp_path, failing_tests=True)
    _fake_github(monkeypatch)
    result = await prepare_issue_fix(
        "acme/demo", 42, _patch(), workspace_path=root, profile="local_trusted"
    )
    assert result["status"] == "partial"
    assert result["data"]["final_result"]["status"] == "partial_completed"
    assert result["data"]["final_result"]["pr_ready"] is False
    assert result["data"]["pr_draft"]["status"] == "NOT_PR_READY"
    assert result["data"]["verification_report"]["failed"]


@pytest.mark.asyncio
async def test_stale_issue_or_base_blocks_publish_before_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _repo(tmp_path)
    _fake_github(monkeypatch)
    prepared = await prepare_issue_fix(
        "acme/demo", 42, _patch(), workspace_path=root, profile="local_trusted"
    )
    task_id = prepared["data"]["task_id"]
    stored = GitHubIssueContext.from_dict(prepared["data"]["context"])
    stale = replace(stored, base_sha="0" * 40)
    monkeypatch.setattr("runtime.github_issue.fetch_issue_context", lambda *args, **kwargs: stale)
    pushed = False

    def should_not_push(*args, **kwargs):
        nonlocal pushed
        pushed = True

    monkeypatch.setattr("runtime.github_issue._push_remote", should_not_push)
    result = await publish_issue_draft(task_id, workspace_path=root, approved=True)
    assert result["status"] == "failed"
    assert result["data"]["stale"] is True
    assert result["data"]["remote_side_effect"] is False
    assert pushed is False


@pytest.mark.asyncio
async def test_unapproved_publish_has_zero_remote_effect(tmp_path: Path) -> None:
    result = await publish_issue_draft("missing-task", workspace_path=tmp_path, approved=False)
    assert result["status"] == "waiting_approval"
    assert result["data"]["remote_side_effect"] is False


@pytest.mark.asyncio
async def test_approved_publish_pushes_and_creates_draft_once_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _repo(tmp_path)
    _fake_github(monkeypatch)
    prepared = await prepare_issue_fix(
        "acme/demo", 42, _patch(), workspace_path=root, profile="local_trusted"
    )
    task_id = prepared["data"]["task_id"]
    stored = GitHubIssueContext.from_dict(prepared["data"]["context"])
    monkeypatch.setattr("runtime.github_issue.fetch_issue_context", lambda *args, **kwargs: stored)
    calls = {"push": 0, "draft": 0}

    def fake_push(worktree: Path, branch: str) -> dict[str, object]:
        calls["push"] += 1
        return {"branch": branch, "pushed": True}

    def fake_draft(context: GitHubIssueContext, payload: dict[str, object]) -> dict[str, object]:
        calls["draft"] += 1
        return {"number": 99, "draft": True, "title": payload["title"]}

    monkeypatch.setattr("runtime.github_issue._push_remote", fake_push)
    monkeypatch.setattr("runtime.github_issue._create_draft_remote", fake_draft)
    first = await publish_issue_draft(task_id, workspace_path=root, approved=True)
    second = await publish_issue_draft(task_id, workspace_path=root, approved=True)
    assert first["status"] == "ok"
    assert second["status"] == "ok"
    assert first["data"]["push_ledger_operation_key"]
    assert first["data"]["draft_ledger_operation_key"]
    assert calls == {"push": 1, "draft": 1}


@pytest.mark.asyncio
async def test_cancelled_prepare_retains_worktree_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _repo(tmp_path)
    _fake_github(monkeypatch)

    def cancel_sensors(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr("runtime.github_issue._run_required_sensors", cancel_sensors)
    with pytest.raises(asyncio.CancelledError):
        await prepare_issue_fix(
            "acme/demo",
            42,
            _patch(),
            workspace_path=root,
            task_id="issue-cancelled",
            profile="local_trusted",
        )
    output = root / ".veya" / "runs" / "issue-cancelled" / "outputs"
    final = json.loads((output / "final_result.json").read_text(encoding="utf-8"))
    context = json.loads((output / "issue_context.json").read_text(encoding="utf-8"))
    assert final["status"] == "cancelled"
    assert "retained" in final["known_limitations"][0]
    assert Path(context["worktree_path"]).is_dir()
