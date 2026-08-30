"""GitHub pull-request review product capability.

This module owns the PR context and review artifact contracts.  It deliberately
does not own semantic orchestration: the MasterAgent decides when to call the
server adapters, while this layer fetches evidence, uses the existing coding
harness, and keeps publication behind an explicit approval flag.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from runtime.coding.tools import coding_worktree_create

ReviewCategory = Literal[
    "correctness",
    "security",
    "runtime",
    "performance",
    "test_gap",
    "maintainability",
    "style",
]
ReviewSeverity = Literal["critical", "high", "medium", "low"]
ReviewVerdict = Literal[
    "APPROVE_READY",
    "COMMENT_READY",
    "REQUEST_CHANGES_READY",
    "INSUFFICIENT_EVIDENCE",
]
ReviewEvent = Literal["comment", "approve", "request_changes"]

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SAFE_TASK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_HIGH_VALUE: frozenset[str] = frozenset({"correctness", "security", "runtime", "test_gap"})


class GitHubPRReviewError(RuntimeError):
    """The requested PR review operation cannot produce trustworthy evidence."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


@dataclass
class GitHubPRContext:
    repo: str
    number: int
    title: str
    body: str
    base_sha: str
    head_sha: str
    changed_files: list[str]
    diff: str
    comments: list[dict[str, Any]] = field(default_factory=list)
    review_history: list[dict[str, Any]] = field(default_factory=list)
    fetched_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(asdict(self)))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GitHubPRContext:
        return cls(
            repo=str(raw["repo"]),
            number=int(raw["number"]),
            title=str(raw.get("title") or ""),
            body=str(raw.get("body") or ""),
            base_sha=str(raw["base_sha"]),
            head_sha=str(raw["head_sha"]),
            changed_files=[str(item) for item in raw.get("changed_files") or []],
            diff=str(raw.get("diff") or ""),
            comments=[dict(item) for item in raw.get("comments") or [] if isinstance(item, dict)],
            review_history=[
                dict(item) for item in raw.get("review_history") or [] if isinstance(item, dict)
            ],
            fetched_at=str(raw.get("fetched_at") or ""),
        )


@dataclass
class ReviewComment:
    file: str
    line: int
    category: ReviewCategory
    severity: ReviewSeverity
    finding: str
    evidence: str
    suggested_fix: str
    confidence: float
    start_line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        value = cast(dict[str, Any], _jsonable(asdict(self)))
        value["range"] = (
            {"start_line": self.start_line, "line": self.line}
            if self.start_line is not None
            else None
        )
        return value


@dataclass
class ReviewArtifact:
    task_id: str
    repo: str
    pr_number: int
    head_sha: str
    verdict: ReviewVerdict
    comments: list[ReviewComment]
    risk_assessment: dict[str, Any]
    verification_report: dict[str, Any]
    artifact_paths: list[str]
    worktree_path: str
    remote_side_effect: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "repo": self.repo,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "verdict": self.verdict,
            "comments": [item.to_dict() for item in self.comments],
            "risk_assessment": _jsonable(self.risk_assessment),
            "verification_report": _jsonable(self.verification_report),
            "artifact_paths": list(self.artifact_paths),
            "worktree_path": self.worktree_path,
            "remote_side_effect": self.remote_side_effect,
        }


def _validate_repo(repo: str) -> str:
    value = str(repo or "").strip()
    if not _REPO.fullmatch(value):
        raise GitHubPRReviewError("repo must use the owner/name form")
    return value


def _validate_number(number: int) -> int:
    try:
        value = int(number)
    except (TypeError, ValueError) as exc:
        raise GitHubPRReviewError("pull request number must be a positive integer") from exc
    if value <= 0:
        raise GitHubPRReviewError("pull request number must be a positive integer")
    return value


def _run(argv: Sequence[str], *, input_text: str | None = None, timeout_s: float = 120) -> str:
    try:
        result = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout_s,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubPRReviewError(f"command failed to start or timed out: {argv[0]}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitHubPRReviewError(f"{' '.join(argv[:3])} failed: {detail[:1000]}")
    return result.stdout


def _gh_json(args: Sequence[str]) -> Any:
    raw = _run(["gh", *args])
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubPRReviewError("GitHub CLI returned invalid JSON") from exc


def _items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        if value and all(isinstance(item, list) for item in value):
            value = [item for page in value for item in page]
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        return [dict(item) for item in value["items"] if isinstance(item, dict)]
    return []


def fetch_pr_context(repo: str, pr_number: int) -> GitHubPRContext:
    """Fetch one immutable-in-this-run PR snapshot without mutating GitHub."""
    repo = _validate_repo(repo)
    number = _validate_number(pr_number)
    view = _gh_json(
        [
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,body,baseRefOid,headRefOid",
        ]
    )
    if not isinstance(view, dict):
        raise GitHubPRReviewError("GitHub PR metadata is not an object")
    files = _items(
        _gh_json(
            ["api", "--paginate", "--slurp", f"repos/{repo}/pulls/{number}/files?per_page=100"]
        )
    )
    issue_comments = _items(
        _gh_json(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repo}/issues/{number}/comments?per_page=100",
            ]
        )
    )
    review_comments = _items(
        _gh_json(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repo}/pulls/{number}/comments?per_page=100",
            ]
        )
    )
    reviews = _items(
        _gh_json(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repo}/pulls/{number}/reviews?per_page=100",
            ]
        )
    )
    for item in issue_comments:
        item["source"] = "issue_comment"
    for item in review_comments:
        item["source"] = "inline_review_comment"
    base_sha = str(view.get("baseRefOid") or view.get("base_sha") or "")
    head_sha = str(view.get("headRefOid") or view.get("head_sha") or "")
    if not base_sha or not head_sha:
        raise GitHubPRReviewError("GitHub PR metadata omitted base or head SHA")
    diff = _run(["gh", "pr", "diff", str(number), "--repo", repo])
    return GitHubPRContext(
        repo=repo,
        number=number,
        title=str(view.get("title") or ""),
        body=str(view.get("body") or ""),
        base_sha=base_sha,
        head_sha=head_sha,
        changed_files=[str(item.get("filename")) for item in files if item.get("filename")],
        diff=diff,
        comments=[*issue_comments, *review_comments],
        review_history=reviews,
    )


def _git(root: Path, args: Sequence[str], *, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubPRReviewError(f"git command failed: {' '.join(args[:2])}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitHubPRReviewError(f"git {' '.join(args[:2])} failed: {detail[:1000]}")
    return result.stdout.strip()


def _git_exists(root: Path, args: Sequence[str]) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _ensure_head_available(root: Path, context: GitHubPRContext) -> None:
    if not _git_exists(root, ["cat-file", "-e", f"{context.head_sha}^{{commit}}"]):
        remote = _git(root, ["remote", "get-url", "origin"], check=False) or "origin"
        _git(
            root,
            [
                "fetch",
                "--no-tags",
                remote,
                f"+refs/pull/{context.number}/head:refs/remotes/origin/veya-pr-{context.number}",
            ],
        )
        if not _git_exists(root, ["cat-file", "-e", f"{context.head_sha}^{{commit}}"]):
            _git(root, ["fetch", "--no-tags", remote, context.head_sha])
    if not _git_exists(root, ["cat-file", "-e", f"{context.head_sha}^{{commit}}"]):
        raise GitHubPRReviewError(f"PR head SHA is not available locally: {context.head_sha}")


def _safe_relative_path(root: Path, relative: str) -> Path | None:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _related_context(worktree: Path, files: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    budget = 100_000
    for relative in files:
        if budget <= 0:
            break
        target = _safe_relative_path(worktree, relative)
        if target is None or not target.is_file():
            continue
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        truncated = len(content) > min(20_000, budget)
        content = content[: min(20_000, budget)]
        budget -= len(content)
        result[relative] = {"content": content, "truncated": truncated}
    return result


@dataclass(frozen=True)
class _AddedLine:
    file: str
    line: int
    text: str


def _added_lines(diff: str) -> list[_AddedLine]:
    lines: list[_AddedLine] = []
    current_file: str | None = None
    new_line = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[6:]
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+([0-9]+)(?:,[0-9]+)?", raw)
            if match:
                new_line = int(match.group(1))
            continue
        if current_file is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            lines.append(_AddedLine(current_file, new_line, raw[1:]))
            new_line += 1
        elif not raw.startswith("-"):
            new_line += 1
    return lines


def _static_review(diff: str) -> list[ReviewComment]:
    """Find only deterministic, high-value regressions visible in added lines."""
    findings: list[ReviewComment] = []
    seen: set[tuple[str, int, str]] = set()

    def add(
        item: _AddedLine,
        category: ReviewCategory,
        severity: ReviewSeverity,
        finding: str,
        suggested_fix: str,
        confidence: float,
    ) -> None:
        key = (item.file, item.line, category)
        if category not in _HIGH_VALUE or key in seen:
            return
        seen.add(key)
        findings.append(
            ReviewComment(
                file=item.file,
                line=max(1, item.line),
                category=category,
                severity=severity,
                finding=finding,
                evidence=item.text.strip()[:500],
                suggested_fix=suggested_fix,
                confidence=confidence,
            )
        )

    added = _added_lines(diff)
    for index, item in enumerate(added):
        text = item.text
        if re.search(r"\bshell\s*=\s*True\b", text) or re.search(r"\bos\.system\s*\(", text):
            add(
                item,
                "security",
                "high",
                "The added process invocation permits shell interpretation of input.",
                "Pass an argv list with shell=False and validate/allowlist every user-controlled argument.",
                0.98,
            )
        elif re.search(r"\beval\s*\(|\bexec\s*\(", text):
            add(
                item,
                "security",
                "high",
                "The added dynamic evaluation executes data as code.",
                "Replace dynamic evaluation with a typed parser or an explicit operation allowlist.",
                0.96,
            )
        elif re.search(r"\bpickle\.loads\s*\(", text) or re.search(r"\byaml\.load\s*\(", text):
            add(
                item,
                "security",
                "high",
                "The added deserialization path can execute or construct unsafe objects.",
                "Use a safe format/loader and validate the resulting schema before use.",
                0.91,
            )
        elif re.search(r"\bverify\s*=\s*False\b", text):
            add(
                item,
                "security",
                "high",
                "The added transport call disables certificate verification.",
                "Keep certificate verification enabled and configure a trusted CA when needed.",
                0.99,
            )
        elif (
            (
                re.search(r"except\s+Exception\s*:\s*$", text)
                or re.search(r"except\s+Exception\s+as\s+\w+\s*:\s*$", text)
            )
            and index + 1 < len(added)
            and added[index + 1].file == item.file
            and (added[index + 1].line == item.line + 1 and added[index + 1].text.strip() == "pass")
        ):
            add(
                item,
                "correctness",
                "medium",
                "The added broad exception handler may hide failures from the caller.",
                "Catch the expected exception types and preserve an observable error or evidence path.",
                0.82,
            )
        elif re.search(r"\btime\.sleep\s*\(", text):
            add(
                item,
                "runtime",
                "medium",
                "The added blocking sleep can stall an async/runtime worker.",
                "Use the runtime's awaited sleep or move blocking work behind the existing executor boundary.",
                0.84,
            )
    return findings


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, value: Any) -> None:
    _write_text(path, json.dumps(_jsonable(value), ensure_ascii=False, indent=2) + "\n")


def _verification(
    root: Path,
    worktree: Path,
    *,
    profile: str,
    run_verification: bool,
) -> dict[str, Any]:
    from runtime.coding.workspace_detect import detect_workspace
    from runtime.harness.guides import load_guides
    from runtime.harness.sensors import run_sensor, sensors_for_workspace

    workspace = detect_workspace(root)
    guides = load_guides(workspace)
    sensors = [
        sensor
        for sensor in sensors_for_workspace(workspace, guides)
        if sensor.required and sensor.kind in {"test", "lint", "typecheck"}
    ]
    results: list[dict[str, Any]] = []
    if run_verification:
        for sensor in sensors:
            results.append(
                run_sensor(
                    sensor,
                    worktree,
                    profile=profile,
                    approved=False,
                    run_id=_task_id_from_worktree(worktree),
                ).to_dict()
            )
    result_ids = {str(item.get("sensor_id")) for item in results}
    not_run = [sensor.id for sensor in sensors if sensor.id not in result_ids]
    failed = [str(item.get("sensor_id")) for item in results if str(item.get("status")) != "passed"]
    return {
        "status": "passed" if sensors and not not_run and not failed else "insufficient_evidence",
        "required_sensor_ids": [sensor.id for sensor in sensors],
        "required_sensor_count": len(sensors),
        "not_run": not_run,
        "failed": failed,
        "results": results,
        "commands": [sensor.command for sensor in sensors],
        "profile": profile,
        "run_verification": run_verification,
    }


def _task_id_from_worktree(worktree: Path) -> str:
    if not worktree.name.startswith("task-"):
        raise GitHubPRReviewError("review sensors require a Veya task worktree")
    task_id = worktree.name.removeprefix("task-")
    if not _SAFE_TASK.fullmatch(task_id):
        raise GitHubPRReviewError("review task id is not safe")
    return task_id


def _verdict(comments: list[ReviewComment], verification: dict[str, Any]) -> ReviewVerdict:
    if verification.get("not_run") or verification.get("failed"):
        return "REQUEST_CHANGES_READY" if verification.get("failed") else "INSUFFICIENT_EVIDENCE"
    if comments:
        return (
            "REQUEST_CHANGES_READY"
            if any(item.severity in {"critical", "high"} for item in comments)
            else "COMMENT_READY"
        )
    return "APPROVE_READY"


def _risk(comments: list[ReviewComment], verification: dict[str, Any]) -> dict[str, Any]:
    categories = {category: 0 for category in sorted(_HIGH_VALUE)}
    for comment in comments:
        categories[comment.category] += 1
    if any(item.severity in {"critical", "high"} for item in comments):
        level = "high"
    elif comments or verification.get("failed"):
        level = "medium"
    else:
        level = "low"
    return {
        "risk_level": level,
        "comment_count": len(comments),
        "comments_by_category": categories,
        "high_value_only": True,
        "style_comments_suppressed": True,
    }


def _summary(
    context: GitHubPRContext,
    artifact: ReviewArtifact,
    verification: dict[str, Any],
) -> str:
    lines = [
        "# GitHub PR review (draft)",
        "",
        f"- Repository: `{context.repo}`",
        f"- PR: `#{context.number}` {context.title}",
        f"- Reviewed head: `{context.head_sha}`",
        f"- Verdict: `{artifact.verdict}`",
        f"- Risk: `{artifact.risk_assessment['risk_level']}`",
        "- Publication: not performed; explicit approval is required.",
        "",
        "## Verification",
        f"- Status: `{verification['status']}`",
        f"- Required sensors: {verification['required_sensor_count']}",
        f"- Failed: {', '.join(verification['failed']) or '(none)'}",
        f"- Not run: {', '.join(verification['not_run']) or '(none)'}",
        "",
        "## Findings",
    ]
    if not artifact.comments:
        lines.append(
            "No high-value inline findings. Style-only observations are suppressed by default."
        )
    else:
        for index, comment in enumerate(artifact.comments, start=1):
            lines.extend(
                [
                    f"### {index}. {comment.severity.upper()} {comment.category}",
                    f"- Location: `{comment.file}:{comment.line}`",
                    f"- Finding: {comment.finding}",
                    f"- Evidence: `{comment.evidence}`",
                    f"- Suggested fix: {comment.suggested_fix}",
                    f"- Confidence: {comment.confidence:.2f}",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def prepare_pr_review(
    repo: str,
    pr_number: int,
    *,
    workspace_path: str | Path = ".",
    task_id: str | None = None,
    profile: str = "local_restricted",
    run_verification: bool = True,
) -> dict[str, Any]:
    """Fetch, isolate, verify, and write a non-published review artifact."""
    repo = _validate_repo(repo)
    number = _validate_number(pr_number)
    root = Path(workspace_path).expanduser().resolve()
    if not (root / ".git").exists():
        raise GitHubPRReviewError(f"workspace is not a Git repository: {root}")
    context = fetch_pr_context(repo, number)
    if task_id is None:
        task_id = f"pr-review-{number}-{context.head_sha[:12]}-{uuid.uuid4().hex[:6]}"
    if not _SAFE_TASK.fullmatch(task_id):
        raise GitHubPRReviewError("task_id must be a single safe path component")
    _ensure_head_available(root, context)
    created = coding_worktree_create(
        str(root),
        task_id,
        f"Review GitHub PR {repo}#{number}: {context.title}",
        base_ref=context.head_sha,
        branch_name=f"veya/pr-review-{number}-{context.head_sha[:12]}-{task_id[-6:]}",
    )
    if created.get("status") != "ok":
        raise GitHubPRReviewError(str(created.get("evidence") or "worktree creation failed"))
    worktree_data = dict(created.get("data", {}).get("worktree") or {})
    worktree_path = Path(str(worktree_data.get("path") or "")).resolve()
    if not worktree_path.is_dir():
        raise GitHubPRReviewError("worktree creation returned no usable path")
    checked_out = _git(worktree_path, ["rev-parse", "HEAD"])
    if checked_out != context.head_sha:
        raise GitHubPRReviewError(
            f"isolated worktree checked out {checked_out}, expected {context.head_sha}"
        )
    related = _related_context(worktree_path, context.changed_files)
    verification = _verification(
        root,
        worktree_path,
        profile=profile,
        run_verification=run_verification,
    )
    comments = _static_review(context.diff)
    risk = _risk(comments, verification)
    verdict = _verdict(comments, verification)
    output_root = root / ".veya" / "runs" / task_id / "outputs"
    artifact_names = [
        "pr_context.json",
        "review_summary.md",
        "inline_comments.json",
        "risk_assessment.json",
        "verification_report.json",
    ]
    context_payload = {
        **context.to_dict(),
        "task_id": task_id,
        "project_root": str(root),
        "worktree_path": str(worktree_path),
        "related_context": related,
        "harness_contract": created.get("data", {}).get("harness_contract"),
    }
    _write_json(output_root / "pr_context.json", context_payload)
    _write_json(output_root / "inline_comments.json", [item.to_dict() for item in comments])
    _write_json(output_root / "risk_assessment.json", risk)
    _write_json(output_root / "verification_report.json", verification)
    artifact = ReviewArtifact(
        task_id=task_id,
        repo=repo,
        pr_number=number,
        head_sha=context.head_sha,
        verdict=verdict,
        comments=comments,
        risk_assessment=risk,
        verification_report=verification,
        artifact_paths=[str(output_root / name) for name in artifact_names],
        worktree_path=str(worktree_path),
    )
    _write_text(output_root / "review_summary.md", _summary(context, artifact, verification))
    manifest_items = []
    for name in artifact_names:
        path = output_root / name
        if path.exists():
            manifest_items.append(
                {
                    "name": name,
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size_bytes": path.stat().st_size,
                    "status": "draft",
                }
            )
    manifest_items.append(
        {
            "name": "artifact_manifest.json",
            "path": str(output_root / "artifact_manifest.json"),
            "sha256": None,
            "size_bytes": None,
            "status": "draft",
        }
    )
    manifest = {
        "task_id": task_id,
        "producer": "github_pr_review_prepare",
        "review_artifact": artifact.to_dict(),
        "artifacts": manifest_items,
        "remote_side_effect": False,
    }
    _write_json(output_root / "artifact_manifest.json", manifest)
    return {
        "status": "ok",
        "data": {
            "task_id": task_id,
            "review_artifact": artifact.to_dict(),
            "context": context.to_dict(),
            "coding_task": created.get("data", {}).get("task"),
            "worktree": worktree_data,
            "verification_report": verification,
            "risk_assessment": risk,
            "related_context_files": sorted(related),
        },
        "artifacts": [
            {"path": str(output_root / name), "kind": name.removesuffix(".json")}
            for name in [*artifact_names, "artifact_manifest.json"]
        ],
        "evidence": [
            {
                "kind": "github_pr_review_prepared",
                "repo": repo,
                "pr_number": number,
                "head_sha": context.head_sha,
                "checked_out_sha": checked_out,
                "remote_side_effect": False,
            }
        ],
        "side_effect": True,
        "requires_approval": False,
    }


def _load_review_inputs(
    workspace_path: str | Path, task_id: str
) -> tuple[Path, GitHubPRContext, list[dict[str, Any]], dict[str, Any]]:
    if not _SAFE_TASK.fullmatch(task_id or ""):
        raise GitHubPRReviewError("task_id must be a single safe path component")
    root = Path(workspace_path).expanduser().resolve()
    output_root = root / ".veya" / "runs" / task_id / "outputs"
    context_path = output_root / "pr_context.json"
    comments_path = output_root / "inline_comments.json"
    summary_path = output_root / "review_summary.md"
    if not context_path.is_file() or not comments_path.is_file() or not summary_path.is_file():
        raise GitHubPRReviewError(f"review artifacts are incomplete for task {task_id}")
    try:
        context = GitHubPRContext.from_dict(json.loads(context_path.read_text(encoding="utf-8")))
        comments_raw = json.loads(comments_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GitHubPRReviewError(f"review artifacts are unreadable for task {task_id}") from exc
    comments = [dict(item) for item in comments_raw if isinstance(item, dict)]
    return root, context, comments, {"body": summary_path.read_text(encoding="utf-8")}


def _post_payload(
    context: GitHubPRContext,
    comments: list[dict[str, Any]],
    *,
    event: ReviewEvent,
    body: str,
) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    for comment in comments:
        file = str(comment.get("file") or "")
        line = int(comment.get("line") or 0)
        if not file or line <= 0 or file not in context.changed_files:
            raise GitHubPRReviewError(f"inline comment has invalid PR location: {file}:{line}")
        item = {
            "path": file,
            "line": line,
            "side": "RIGHT",
            "body": (
                f"**{comment.get('category') or 'review'} / "
                f"{comment.get('severity') or 'medium'}**\n\n"
                f"{comment.get('finding') or ''}\n\n"
                f"Evidence: `{comment.get('evidence') or ''}`\n\n"
                f"Suggested fix: {comment.get('suggested_fix') or ''}\n\n"
                f"Confidence: {float(comment.get('confidence') or 0):.2f}"
            ),
        }
        if comment.get("start_line"):
            item["start_line"] = int(comment["start_line"])
            item["start_side"] = "RIGHT"
        normalized.append(item)
    return {
        "commit_id": context.head_sha,
        "body": body[:60_000],
        "event": event.upper().replace("_", "_"),
        "comments": normalized,
    }


def _post_remote(context: GitHubPRContext, payload: dict[str, Any]) -> dict[str, Any]:
    endpoint = f"repos/{context.repo}/pulls/{context.number}/reviews"
    raw = _run(
        ["gh", "api", endpoint, "--method", "POST", "--input", "-"],
        input_text=json.dumps(payload, ensure_ascii=False),
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = {"raw": raw[:2000]}
    if not isinstance(value, dict):
        value = {"response": value}
    return {
        "id": value.get("id"),
        "html_url": value.get("html_url"),
        "event": payload["event"],
        "comment_count": len(payload["comments"]),
    }


async def post_pr_review(
    task_id: str,
    *,
    workspace_path: str | Path = ".",
    event: ReviewEvent = "comment",
    approved: bool = False,
    body: str | None = None,
) -> dict[str, Any]:
    """Publish only after explicit approval and a fresh head-SHA check."""
    if not approved:
        return {
            "status": "waiting_approval",
            "data": {"task_id": task_id, "remote_side_effect": False},
            "evidence": [
                {
                    "kind": "github_pr_review_not_posted",
                    "reason": "explicit approval is required",
                    "remote_side_effect": False,
                }
            ],
            "artifacts": [],
            "side_effect": False,
            "requires_approval": True,
        }
    if event not in {"comment", "approve", "request_changes"}:
        raise GitHubPRReviewError("event must be comment, approve, or request_changes")
    root, stored, comments, summary = _load_review_inputs(workspace_path, task_id)
    fresh = await asyncio.to_thread(fetch_pr_context, stored.repo, stored.number)
    if fresh.head_sha != stored.head_sha:
        return {
            "status": "failed",
            "data": {"task_id": task_id, "stale": True, "remote_side_effect": False},
            "evidence": [
                {
                    "kind": "stale_pr_head",
                    "stored_head_sha": stored.head_sha,
                    "current_head_sha": fresh.head_sha,
                    "action": "discard artifact and re-fetch/re-prepare",
                    "remote_side_effect": False,
                }
            ],
            "artifacts": [],
            "side_effect": False,
            "requires_approval": False,
        }
    payload = _post_payload(
        stored,
        comments,
        event=event,
        body=body or str(summary["body"]),
    )
    from runtime.execution.durable import DurableExecutionRepository
    from runtime.execution.side_effects import SideEffectLedger

    repository = DurableExecutionRepository(
        sqlite_path=root / ".veya" / "execution-runtime.sqlite3"
    )
    await repository.connect()
    operation_key = (
        f"veya:github-pr-review:{stored.repo}:{stored.number}:{stored.head_sha}:"
        f"{hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]}"
    )
    try:
        remote = await SideEffectLedger(repository).execute(
            goal_run_id=f"pr-review:{task_id}",
            work_item_id=f"post-review:{task_id}",
            operation_key=operation_key,
            operation_type="github.pull_request_review",
            target_ref=f"github:{stored.repo}#{stored.number}",
            request=payload,
            capability="manual_only",
            provider=lambda: asyncio.to_thread(_post_remote, stored, payload),
        )
    except Exception as exc:
        return {
            "status": "failed",
            "data": {"task_id": task_id, "remote_side_effect": "unknown"},
            "evidence": [
                {
                    "kind": "github_pr_review_post_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "side_effect_ledger_operation_key": operation_key,
                }
            ],
            "artifacts": [],
            "side_effect": True,
            "requires_approval": False,
        }
    finally:
        await repository.close()
    return {
        "status": "ok",
        "data": {
            "task_id": task_id,
            "remote": remote,
            "side_effect_ledger_operation_key": operation_key,
            "remote_side_effect": True,
        },
        "evidence": [
            {
                "kind": "github_pr_review_posted",
                "event": event,
                "repo": stored.repo,
                "pr_number": stored.number,
                "head_sha": stored.head_sha,
                "side_effect_ledger_operation_key": operation_key,
                "remote_side_effect": True,
            }
        ],
        "artifacts": [],
        "side_effect": True,
        "requires_approval": False,
    }


__all__ = [
    "GitHubPRContext",
    "GitHubPRReviewError",
    "ReviewArtifact",
    "ReviewComment",
    "fetch_pr_context",
    "post_pr_review",
    "prepare_pr_review",
]
