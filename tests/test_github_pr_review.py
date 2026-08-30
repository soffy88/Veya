from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from runtime.github_pr import (
    GitHubPRContext,
    ReviewComment,
    _static_review,
    _verdict,
    post_pr_review,
    prepare_pr_review,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _repo(tmp_path: Path, *, changed: str = "print('base')\n") -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "PR Review Tests")
    (root / "app.py").write_text("print('base')\n", encoding="utf-8")
    (root / "AGENTS.md").write_text(
        f"## TEST\n- test: {sys.executable} -c \"print('tests pass')\"\n"
        f"## LINT\n- lint: {sys.executable} -c \"print('lint pass')\"\n"
        f"## TYPECHECK\n- typecheck: {sys.executable} -c \"print('types pass')\"\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    base_sha = _git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text(changed, encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-m", "pull request")
    return root, base_sha, _git(root, "rev-parse", "HEAD")


def _fake_github(monkeypatch: pytest.MonkeyPatch, base_sha: str, head_sha: str, diff: str) -> None:
    from runtime import github_pr

    def fake_json(args: list[str]) -> object:
        endpoint = " ".join(args)
        if args[:2] == ["pr", "view"]:
            return {
                "number": 7,
                "title": "exercise PR",
                "body": "body",
                "baseRefOid": base_sha,
                "headRefOid": head_sha,
            }
        if "/files?" in endpoint:
            return [{"filename": "app.py", "status": "modified"}]
        if "/issues/" in endpoint or "/pulls/" in endpoint:
            return []
        raise AssertionError(f"unexpected GitHub query: {args}")

    real_run = github_pr._run

    def fake_run(args, **kwargs):
        if list(args)[:3] == ["gh", "pr", "diff"]:
            return diff
        return real_run(args, **kwargs)

    monkeypatch.setattr(github_pr, "_gh_json", fake_json)
    monkeypatch.setattr(github_pr, "_run", fake_run)


def test_static_review_finds_security_regression_and_clean_diff_is_empty() -> None:
    diff = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-print('base')
+subprocess.run(user_input, shell=True)
"""
    comments = _static_review(diff)
    assert len(comments) == 1
    assert comments[0].category == "security"
    assert comments[0].severity == "high"
    assert comments[0].file == "app.py"
    assert comments[0].line == 1
    assert {
        "file",
        "line",
        "category",
        "severity",
        "finding",
        "evidence",
        "suggested_fix",
        "confidence",
    } <= set(comments[0].to_dict())
    assert _static_review("--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-print(1)\n+print(2)\n") == []
    assert _static_review(
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n+except Exception:\n+    pass\n"
    )


def test_prepare_dogfood_fetches_checks_and_writes_review_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, base_sha, head_sha = _repo(
        tmp_path, changed="import subprocess\nsubprocess.run(user_input, shell=True)\n"
    )
    diff = """--- a/app.py
+++ b/app.py
@@ -1 +1,2 @@
-print('base')
+import subprocess
+subprocess.run(user_input, shell=True)
"""
    _fake_github(monkeypatch, base_sha, head_sha, diff)

    result = prepare_pr_review(
        "acme/demo",
        7,
        workspace_path=root,
        profile="local_trusted",
    )

    assert result["status"] == "ok"
    data = result["data"]
    assert data["context"]["head_sha"] == head_sha
    assert data["coding_task"]["worktree_path"] == data["worktree"]["path"]
    assert data["worktree"]["path"]
    artifact_root = root / ".veya" / "runs" / data["task_id"] / "outputs"
    assert {
        "pr_context.json",
        "review_summary.md",
        "inline_comments.json",
        "risk_assessment.json",
        "verification_report.json",
        "artifact_manifest.json",
    } <= {path.name for path in artifact_root.iterdir()}
    assert json.loads((artifact_root / "inline_comments.json").read_text())
    verification = json.loads((artifact_root / "verification_report.json").read_text())
    assert verification["status"] == "passed"
    assert "Publication: not performed" in (artifact_root / "review_summary.md").read_text()


@pytest.mark.asyncio
async def test_unapproved_post_has_zero_remote_side_effect(tmp_path: Path, monkeypatch) -> None:
    called = False

    def should_not_post(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("runtime.github_pr._post_remote", should_not_post)
    result = await post_pr_review("missing-task", workspace_path=tmp_path, approved=False)
    assert result["status"] == "waiting_approval"
    assert result["data"]["remote_side_effect"] is False
    assert called is False


def _write_review_inputs(
    root: Path, task_id: str, context: GitHubPRContext, comments: list[dict]
) -> None:
    output = root / ".veya" / "runs" / task_id / "outputs"
    output.mkdir(parents=True)
    payload = {**context.to_dict(), "task_id": task_id}
    (output / "pr_context.json").write_text(json.dumps(payload), encoding="utf-8")
    (output / "inline_comments.json").write_text(json.dumps(comments), encoding="utf-8")
    (output / "review_summary.md").write_text("# draft\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_stale_head_invalidates_review_without_post(tmp_path: Path, monkeypatch) -> None:
    context = GitHubPRContext("acme/demo", 7, "title", "", "base", "head-old", ["app.py"], "")
    _write_review_inputs(tmp_path, "review-7", context, [])
    fresh = GitHubPRContext("acme/demo", 7, "title", "", "base", "head-new", ["app.py"], "")
    posted = False

    async def no_post(*args, **kwargs):
        nonlocal posted
        posted = True

    monkeypatch.setattr("runtime.github_pr.fetch_pr_context", lambda repo, number: fresh)
    monkeypatch.setattr("runtime.github_pr._post_remote", no_post)
    result = await post_pr_review("review-7", workspace_path=tmp_path, approved=True)
    assert result["status"] == "failed"
    assert result["data"]["stale"] is True
    assert posted is False


@pytest.mark.asyncio
async def test_approved_post_records_side_effect_ledger(tmp_path: Path, monkeypatch) -> None:
    context = GitHubPRContext("acme/demo", 7, "title", "", "base", "head", ["app.py"], "")
    comment = ReviewComment(
        file="app.py",
        line=1,
        category="security",
        severity="high",
        finding="unsafe",
        evidence="shell=True",
        suggested_fix="use argv",
        confidence=0.99,
    )
    _write_review_inputs(tmp_path, "review-8", context, [comment.to_dict()])
    monkeypatch.setattr("runtime.github_pr.fetch_pr_context", lambda repo, number: context)
    monkeypatch.setattr(
        "runtime.github_pr._post_remote",
        lambda context, payload: {"id": 123, "event": payload["event"]},
    )
    result = await post_pr_review("review-8", workspace_path=tmp_path, approved=True)
    assert result["status"] == "ok"
    assert result["data"]["remote_side_effect"] is True
    assert result["data"]["side_effect_ledger_operation_key"]


def test_failed_or_missing_required_sensor_never_approves() -> None:
    assert _verdict([], {"required_sensor_count": 1, "failed": ["test"], "not_run": []}) == (
        "REQUEST_CHANGES_READY"
    )
    assert _verdict([], {"required_sensor_count": 1, "failed": [], "not_run": ["test"]}) == (
        "INSUFFICIENT_EVIDENCE"
    )
