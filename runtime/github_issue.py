"""GitHub Issue to verified patch product capability.

The Issue workflow is an additive MasterAgent capability.  It reuses the
existing coding worktree and Harness sensor boundaries, keeps the proposed
patch uncommitted during preparation, and puts every remote mutation behind
the durable SideEffectLedger plus an explicit approval flag.
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

from runtime.coding.models import CodingTask
from runtime.coding.tools import coding_apply_patch, coding_worktree_create
from runtime.coding.workspace_detect import detect_workspace
from runtime.coding.worktree import WorktreeManager, repo_root_for_worktree

IssueWorkflowStatus = Literal["completed", "partial_completed", "failed", "cancelled"]

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,150}$")
_TASK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


class GitHubIssueWorkflowError(RuntimeError):
    """The Issue workflow cannot produce or publish trustworthy evidence."""


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
class GitHubIssueContext:
    repo: str
    number: int
    title: str
    body: str
    labels: list[str]
    comments: list[dict[str, Any]]
    referenced_files: list[str]
    linked_prs: list[dict[str, Any]]
    base_branch: str
    base_sha: str
    updated_at: str
    issue_fingerprint: str
    fetched_at: str = field(default_factory=_now)

    @property
    def issue_url(self) -> str:
        return f"https://github.com/{self.repo}/issues/{self.number}"

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(asdict(self)))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GitHubIssueContext:
        return cls(
            repo=str(raw["repo"]),
            number=int(raw["number"]),
            title=str(raw.get("title") or ""),
            body=str(raw.get("body") or ""),
            labels=[str(item) for item in raw.get("labels") or []],
            comments=[dict(item) for item in raw.get("comments") or [] if isinstance(item, dict)],
            referenced_files=[str(item) for item in raw.get("referenced_files") or []],
            linked_prs=[
                dict(item) for item in raw.get("linked_prs") or [] if isinstance(item, dict)
            ],
            base_branch=str(raw["base_branch"]),
            base_sha=str(raw["base_sha"]),
            updated_at=str(raw.get("updated_at") or ""),
            issue_fingerprint=str(raw["issue_fingerprint"]),
            fetched_at=str(raw.get("fetched_at") or ""),
        )


def _validate_repo(repo: str) -> str:
    value = str(repo or "").strip()
    if not _REPO.fullmatch(value):
        raise GitHubIssueWorkflowError("repo must use the owner/name form")
    return value


def _validate_number(number: int) -> int:
    try:
        value = int(number)
    except (TypeError, ValueError) as exc:
        raise GitHubIssueWorkflowError("issue number must be a positive integer") from exc
    if value <= 0:
        raise GitHubIssueWorkflowError("issue number must be a positive integer")
    return value


def _validate_branch(branch: str) -> str:
    value = str(branch or "").strip()
    if (
        not _BRANCH.fullmatch(value)
        or value.startswith(("/", "-"))
        or ".." in value
        or "//" in value
        or value.endswith(("/", ".lock"))
    ):
        raise GitHubIssueWorkflowError(f"invalid branch name: {branch!r}")
    return value


def _validate_task(task_id: str) -> str:
    value = str(task_id or "").strip()
    if not _TASK.fullmatch(value) or value in {".", ".."}:
        raise GitHubIssueWorkflowError("task_id must be a single safe path component")
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
        raise GitHubIssueWorkflowError(f"command failed to start or timed out: {argv[0]}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitHubIssueWorkflowError(f"{' '.join(argv[:3])} failed: {detail[:1000]}")
    return result.stdout


def _gh_json(args: Sequence[str]) -> Any:
    raw = _run(["gh", *args])
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubIssueWorkflowError("GitHub CLI returned invalid JSON") from exc


def _items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        if value and all(isinstance(item, list) for item in value):
            value = [item for page in value for item in page]
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        return [dict(item) for item in value["items"] if isinstance(item, dict)]
    return []


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
        raise GitHubIssueWorkflowError(f"git command failed: {' '.join(args[:2])}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitHubIssueWorkflowError(f"git {' '.join(args[:2])} failed: {detail[:1000]}")
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


def _default_branch(repo: str) -> str:
    try:
        value = _gh_json(["repo", "view", repo, "--json", "defaultBranchRef"])
    except GitHubIssueWorkflowError:
        # A caller may provide an explicit base branch.  The fallback keeps
        # local fixture repositories useful without weakening publish checks.
        return "main"
    branch = value.get("defaultBranchRef", {}).get("name") if isinstance(value, dict) else None
    return _validate_branch(str(branch or "main"))


def _base_sha(root: Path, branch: str) -> str:
    remote = _git(root, ["ls-remote", "origin", f"refs/heads/{branch}"], check=False)
    if remote:
        candidate = remote.split()[0]
        if re.fullmatch(r"[0-9a-fA-F]{40}", candidate):
            return candidate
    for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}", "HEAD"):
        value = _git(root, ["rev-parse", "--verify", ref], check=False)
        if value:
            return value
    raise GitHubIssueWorkflowError(f"cannot resolve repository base SHA for {branch}")


def _ensure_base_available(root: Path, context: GitHubIssueContext) -> None:
    if _git_exists(root, ["cat-file", "-e", f"{context.base_sha}^{{commit}}"]):
        return
    remote = _git(root, ["remote", "get-url", "origin"], check=False) or "origin"
    _git(
        root,
        [
            "fetch",
            "--no-tags",
            remote,
            f"+refs/heads/{context.base_branch}:refs/remotes/origin/{context.base_branch}",
        ],
    )
    if not _git_exists(root, ["cat-file", "-e", f"{context.base_sha}^{{commit}}"]):
        raise GitHubIssueWorkflowError(f"repository base SHA is not available: {context.base_sha}")


def _label_names(raw: Any) -> list[str]:
    values: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                values.append(str(item["name"]))
            elif isinstance(item, str):
                values.append(item)
    return sorted(dict.fromkeys(values))


def _comment_text(comments: list[dict[str, Any]]) -> str:
    return "\n".join(str(item.get("body") or "") for item in comments)


def _referenced_files(text: str) -> list[str]:
    found: set[str] = set()
    token_pattern = re.compile(
        r"(?<![A-Za-z0-9_])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.(?:py|pyi|js|jsx|ts|tsx|rs|go|java|kt|rb|php|sql|yaml|yml|json|toml|md))(?:[:#][0-9]+)?"
    )
    for match in token_pattern.finditer(text):
        value = match.group(1).strip("`'\".,;()[]{}")
        if value.startswith(("http://", "https://")):
            continue
        found.add(value)
    return sorted(found)


def _linked_prs(
    repo: str, number: int, timeline: list[dict[str, Any]], text: str
) -> list[dict[str, Any]]:
    linked: dict[int, dict[str, Any]] = {}
    for item in timeline:
        source_value = item.get("source")
        source: dict[str, Any] = source_value if isinstance(source_value, dict) else {}
        issue_value = source.get("issue")
        issue: dict[str, Any] = issue_value if isinstance(issue_value, dict) else {}
        pull_value = issue.get("pull_request")
        pull: dict[str, Any] = pull_value if isinstance(pull_value, dict) else {}
        value = issue.get("number") or item.get("number")
        if pull and value:
            linked[int(value)] = {
                "number": int(value),
                "url": str(issue.get("html_url") or pull.get("html_url") or ""),
                "title": str(issue.get("title") or ""),
            }
    pattern = re.compile(rf"https://github\.com/{re.escape(repo)}/pull/(\d+)")
    for match in pattern.finditer(text):
        value = int(match.group(1))
        linked.setdefault(value, {"number": value, "url": match.group(0)})
    return [linked[key] for key in sorted(linked)]


def _fingerprint(
    repo: str,
    number: int,
    title: str,
    body: str,
    labels: list[str],
    comments: list[dict[str, Any]],
    referenced_files: list[str],
    linked_prs: list[dict[str, Any]],
    updated_at: str,
) -> str:
    payload = {
        "repo": repo,
        "number": number,
        "title": title,
        "body": body,
        "labels": labels,
        "comments": comments,
        "referenced_files": referenced_files,
        "linked_prs": linked_prs,
        "updated_at": updated_at,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def fetch_issue_context(
    repo: str,
    issue_number: int,
    *,
    workspace_path: str | Path = ".",
    base_branch: str | None = None,
) -> GitHubIssueContext:
    """Fetch Issue metadata, discussion, references, links, and base revision."""
    repo = _validate_repo(repo)
    number = _validate_number(issue_number)
    root = Path(workspace_path).expanduser().resolve()
    view = _gh_json(
        [
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,body,labels,comments,updatedAt",
        ]
    )
    if not isinstance(view, dict):
        raise GitHubIssueWorkflowError("GitHub Issue metadata is not an object")
    branch = _validate_branch(base_branch or _default_branch(repo))
    comments = [dict(item) for item in view.get("comments") or [] if isinstance(item, dict)]
    body = str(view.get("body") or "")
    discussion = f"{body}\n{_comment_text(comments)}"
    timeline = _items(
        _gh_json(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repo}/issues/{number}/timeline?per_page=100",
            ]
        )
    )
    labels = _label_names(view.get("labels"))
    referenced = _referenced_files(discussion)
    linked = _linked_prs(repo, number, timeline, discussion)
    title = str(view.get("title") or "")
    updated_at = str(view.get("updatedAt") or view.get("updated_at") or "")
    return GitHubIssueContext(
        repo=repo,
        number=number,
        title=title,
        body=body,
        labels=labels,
        comments=comments,
        referenced_files=referenced,
        linked_prs=linked,
        base_branch=branch,
        base_sha=_base_sha(root, branch),
        updated_at=updated_at,
        issue_fingerprint=_fingerprint(
            repo,
            number,
            title,
            body,
            labels,
            comments,
            referenced,
            linked,
            updated_at,
        ),
    )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, value: Any) -> None:
    _write_text(path, json.dumps(_jsonable(value), ensure_ascii=False, indent=2) + "\n")


def _output_root(root: Path, task_id: str) -> Path:
    return root / ".veya" / "runs" / _validate_task(task_id) / "outputs"


def _worktree_diff(worktree: Path) -> tuple[str, list[str]]:
    diff = WorktreeManager(repo_root_for_worktree(worktree)).diff(path=worktree)
    raw_files = diff.get("changed_files", [])
    files: list[str] = [str(item) for item in raw_files] if isinstance(raw_files, list) else []
    return str(diff.get("patch", "")), files


def _run_required_sensors(
    root: Path,
    worktree: Path,
    task_id: str,
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
            try:
                result = run_sensor(
                    sensor,
                    worktree,
                    profile=profile,
                    approved=False,
                    run_id=task_id,
                )
                results.append(result.to_dict())
            except Exception as exc:
                results.append(
                    {
                        "sensor_id": sensor.id,
                        "status": "error",
                        "required": True,
                        "command": sensor.command,
                        "message": f"{type(exc).__name__}: {exc}",
                    }
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


def _branch_for_issue(context: GitHubIssueContext, task_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", context.title.lower()).strip("-")[:42] or "fix"
    return _validate_branch(f"veya/issue-{context.number}-{slug}-{task_id[-8:]}")


def _draft_payload(
    context: GitHubIssueContext,
    *,
    branch: str,
    files: list[str],
    verification: dict[str, Any],
    known_limitations: list[str],
    status: str,
) -> dict[str, Any]:
    title = f"fix: {context.title}".strip()[:200] or f"fix: issue #{context.number}"
    passed = verification.get("status") == "passed"
    body = (
        f"Fixes {context.issue_url}\n\n"
        "This Draft PR was prepared by Veya from the verified Issue workflow.\n\n"
        f"Required sensors: {'PASS' if passed else 'NOT PASS'}\n"
        f"Changed files: {', '.join(files) or '(none)'}\n"
        "Publication remains approval-gated."
    )
    return {
        "status": status,
        "title": title,
        "body": body,
        "base_branch": context.base_branch,
        "head_branch": branch,
        "issue_reference": {
            "repo": context.repo,
            "number": context.number,
            "url": context.issue_url,
        },
        "files_changed": files,
        "verification_summary": {
            "status": verification.get("status"),
            "required_sensor_count": verification.get("required_sensor_count", 0),
            "failed": list(verification.get("failed") or []),
            "not_run": list(verification.get("not_run") or []),
        },
        "known_limitations": known_limitations,
        "remote_side_effect": False,
    }


def _manifest(output: Path, task_id: str, producer: str) -> dict[str, Any]:
    names = [
        "issue_context.json",
        "diff.patch",
        "verification_report.json",
        "pr_draft.json",
        "final_result.json",
    ]
    items: list[dict[str, Any]] = []
    for name in names:
        path = output / name
        if path.is_file():
            items.append(
                {
                    "name": name,
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size_bytes": path.stat().st_size,
                    "status": "verified"
                    if name in {"diff.patch", "verification_report.json"}
                    else "draft",
                }
            )
    items.append(
        {
            "name": "artifact_manifest.json",
            "path": str(output / "artifact_manifest.json"),
            "sha256": None,
            "size_bytes": None,
            "status": "draft",
        }
    )
    return {
        "task_id": task_id,
        "producer": producer,
        "artifacts": items,
        "remote_side_effect": False,
    }


def _write_issue_artifacts(
    root: Path,
    task_id: str,
    context: GitHubIssueContext,
    *,
    worktree: Path | None,
    coding_task: CodingTask | None,
    verification: dict[str, Any],
    workflow_status: IssueWorkflowStatus,
    acceptance_passed: bool,
    known_limitations: list[str],
    cancellation_reason: str | None = None,
) -> dict[str, Any]:
    output = _output_root(root, task_id)
    output.mkdir(parents=True, exist_ok=True)
    branch = ""
    files: list[str] = []
    patch = ""
    if worktree is not None and worktree.is_dir():
        branch = _git(worktree, ["branch", "--show-current"], check=False)
        try:
            patch, files = _worktree_diff(worktree)
        except GitHubIssueWorkflowError:
            patch = ""
    issue_payload = {
        **context.to_dict(),
        "task_id": task_id,
        "worktree_path": str(worktree) if worktree else None,
        "coding_task": coding_task.to_dict() if coding_task else None,
    }
    _write_json(output / "issue_context.json", issue_payload)
    _write_text(output / "diff.patch", patch)
    _write_json(output / "verification_report.json", verification)
    status = "PR_READY" if acceptance_passed else "NOT_PR_READY"
    _write_json(
        output / "pr_draft.json",
        _draft_payload(
            context,
            branch=branch,
            files=files,
            verification=verification,
            known_limitations=known_limitations,
            status=status,
        ),
    )
    final_result = {
        "task_id": task_id,
        "workflow": "github_issue_to_verified_patch",
        "status": workflow_status,
        "acceptance_passed": acceptance_passed,
        "pr_ready": acceptance_passed,
        "repo": context.repo,
        "issue_number": context.number,
        "base_branch": context.base_branch,
        "base_sha": context.base_sha,
        "head_branch": branch,
        "files_changed": files,
        "verification_status": verification.get("status"),
        "known_limitations": known_limitations,
        "cancellation_reason": cancellation_reason,
        "remote_side_effect": False,
        "coding_task": coding_task.to_dict() if coding_task else None,
        "generated_at": _now(),
    }
    _write_json(output / "final_result.json", final_result)
    _write_json(
        output / "artifact_manifest.json", _manifest(output, task_id, "github_issue_fix_prepare")
    )
    return {
        "output": output,
        "branch": branch,
        "files": files,
        "patch": patch,
        "final_result": final_result,
    }


async def prepare_issue_fix(
    repo: str,
    issue_number: int,
    patch: str,
    *,
    workspace_path: str | Path = ".",
    task_id: str | None = None,
    base_branch: str | None = None,
    profile: str = "local_restricted",
    run_verification: bool = True,
) -> dict[str, Any]:
    """Fetch an Issue, apply a model-provided patch, verify it, and draft locally."""
    repo = _validate_repo(repo)
    number = _validate_number(issue_number)
    root = Path(workspace_path).expanduser().resolve()
    if not (root / ".git").exists():
        raise GitHubIssueWorkflowError(f"workspace is not a Git repository: {root}")
    if not str(patch or "").strip():
        raise GitHubIssueWorkflowError("patch is required; MasterAgent must provide a unified diff")
    context = fetch_issue_context(
        repo,
        number,
        workspace_path=root,
        base_branch=base_branch,
    )
    resolved_task = _validate_task(task_id or f"issue-fix-{number}-{uuid.uuid4().hex[:10]}")
    _ensure_base_available(root, context)
    branch = _branch_for_issue(context, resolved_task)
    created = coding_worktree_create(
        str(root),
        resolved_task,
        f"Fix GitHub Issue {repo}#{number}: {context.title}",
        base_ref=context.base_sha,
        branch_name=branch,
    )
    if created.get("status") != "ok":
        raise GitHubIssueWorkflowError(str(created.get("evidence") or "worktree creation failed"))
    worktree_data = dict(created.get("data", {}).get("worktree") or {})
    worktree = Path(str(worktree_data.get("path") or "")).resolve()
    if not worktree.is_dir():
        raise GitHubIssueWorkflowError("worktree creation returned no usable path")
    checked_out = _git(worktree, ["rev-parse", "HEAD"])
    if checked_out != context.base_sha:
        raise GitHubIssueWorkflowError(
            f"isolated worktree checked out {checked_out}, expected {context.base_sha}"
        )
    workspace = detect_workspace(root)
    coding_task = CodingTask(
        id=resolved_task,
        workspace_id=workspace.id,
        goal_run_id=None,
        source="github_issue",
        objective=f"Fix GitHub Issue {repo}#{number}: {context.title}",
        issue_ref=f"{repo}#{number}",
        status="running",
        branch_name=branch,
        worktree_path=str(worktree),
    )
    verification: dict[str, Any] = {
        "status": "insufficient_evidence",
        "required_sensor_ids": [],
        "required_sensor_count": 0,
        "not_run": [],
        "failed": [],
        "results": [],
        "commands": [],
        "profile": profile,
        "run_verification": run_verification,
    }
    try:
        applied = coding_apply_patch(str(worktree), patch)
        if applied.get("status") != "ok":
            info = _write_issue_artifacts(
                root,
                resolved_task,
                context,
                worktree=worktree,
                coding_task=coding_task,
                verification=verification,
                workflow_status="failed",
                acceptance_passed=False,
                known_limitations=[
                    "The proposed patch could not be applied to the isolated worktree."
                ],
            )
            return {
                "status": "failed",
                "data": {
                    "task_id": resolved_task,
                    "final_result": info["final_result"],
                    "worktree": worktree_data,
                    "coding_task": coding_task.to_dict(),
                },
                "artifacts": [str(info["output"] / name) for name in _artifact_names()],
                "evidence": [{"kind": "issue_patch_apply_failed", "remote_side_effect": False}],
                "side_effect": True,
                "requires_approval": False,
            }
        verification = await asyncio.to_thread(
            _run_required_sensors,
            root,
            worktree,
            resolved_task,
            profile=profile,
            run_verification=run_verification,
        )
        try:
            _, files = _worktree_diff(worktree)
        except GitHubIssueWorkflowError:
            files = []
        acceptance = bool(files) and verification.get("status") == "passed"
        limitations = []
        if not files:
            limitations.append("The proposed patch produced no tracked diff.")
        if verification.get("failed"):
            limitations.append("One or more required sensors failed.")
        if verification.get("not_run"):
            limitations.append("One or more required sensors did not run.")
        coding_task.status = "completed" if acceptance else "partial_completed"
        info = _write_issue_artifacts(
            root,
            resolved_task,
            context,
            worktree=worktree,
            coding_task=coding_task,
            verification=verification,
            workflow_status="completed" if acceptance else "partial_completed",
            acceptance_passed=acceptance,
            known_limitations=limitations,
        )
        return {
            "status": "ok" if acceptance else "partial",
            "data": {
                "task_id": resolved_task,
                "context": context.to_dict(),
                "coding_task": coding_task.to_dict(),
                "worktree": worktree_data,
                "verification_report": verification,
                "pr_draft": json.loads(
                    (info["output"] / "pr_draft.json").read_text(encoding="utf-8")
                ),
                "final_result": info["final_result"],
            },
            "artifacts": [str(info["output"] / name) for name in _artifact_names()],
            "evidence": [
                {
                    "kind": "github_issue_verified_patch_prepared",
                    "repo": repo,
                    "issue_number": number,
                    "base_sha": context.base_sha,
                    "checked_out_sha": checked_out,
                    "acceptance_passed": acceptance,
                    "remote_side_effect": False,
                }
            ],
            "side_effect": True,
            "requires_approval": False,
        }
    except asyncio.CancelledError:
        _write_issue_artifacts(
            root,
            resolved_task,
            context,
            worktree=worktree,
            coding_task=coding_task,
            verification={**verification, "status": "cancelled"},
            workflow_status="cancelled",
            acceptance_passed=False,
            known_limitations=[
                "Preparation was cancelled; the worktree and artifacts were retained."
            ],
            cancellation_reason="prepare task cancelled",
        )
        raise


def _artifact_names() -> tuple[str, ...]:
    return (
        "issue_context.json",
        "diff.patch",
        "verification_report.json",
        "artifact_manifest.json",
        "pr_draft.json",
        "final_result.json",
    )


def _load_prepared(
    workspace_path: str | Path, task_id: str
) -> tuple[Path, GitHubIssueContext, dict[str, Any], dict[str, Any], dict[str, Any]]:
    root = Path(workspace_path).expanduser().resolve()
    output = _output_root(root, task_id)
    required = {name: output / name for name in _artifact_names()}
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        raise GitHubIssueWorkflowError(
            f"Issue fix artifacts are incomplete for {task_id}: {', '.join(missing)}"
        )
    try:
        context = GitHubIssueContext.from_dict(
            json.loads(required["issue_context.json"].read_text(encoding="utf-8"))
        )
        draft = json.loads(required["pr_draft.json"].read_text(encoding="utf-8"))
        final = json.loads(required["final_result.json"].read_text(encoding="utf-8"))
        verification = json.loads(required["verification_report.json"].read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError) as exc:
        raise GitHubIssueWorkflowError(f"Issue fix artifacts are unreadable for {task_id}") from exc
    context_data = json.loads(required["issue_context.json"].read_text(encoding="utf-8"))
    worktree_value = context_data.get("worktree_path")
    if not worktree_value:
        raise GitHubIssueWorkflowError("Issue fix artifact has no worktree path")
    return (
        root,
        context,
        draft,
        final,
        {"verification": verification, "worktree": Path(worktree_value)},
    )


def _commit_patch(worktree: Path, title: str) -> tuple[str, str]:
    branch = _git(worktree, ["branch", "--show-current"])
    if not branch:
        raise GitHubIssueWorkflowError("cannot publish a detached Issue worktree")
    status = _git(worktree, ["status", "--short", "--untracked-files=all"], check=False)
    if status:
        _git(worktree, ["add", "--all"])
        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "-c",
                    "user.name=Veya Issue Fix",
                    "-c",
                    "user.email=veya-issue-fix@users.noreply.github.com",
                    "commit",
                    "-m",
                    f"fix: {title}"[:200],
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise GitHubIssueWorkflowError("failed to commit verified Issue patch") from exc
    commit = _git(worktree, ["rev-parse", "HEAD"])
    if not commit:
        raise GitHubIssueWorkflowError("verified Issue worktree has no commit")
    return branch, commit


def _push_remote(worktree: Path, branch: str) -> dict[str, Any]:
    _git(worktree, ["push", "--set-upstream", "origin", branch])
    return {"branch": branch, "pushed": True}


def _create_draft_remote(context: GitHubIssueContext, payload: dict[str, Any]) -> dict[str, Any]:
    raw = _run(
        ["gh", "api", f"repos/{context.repo}/pulls", "--method", "POST", "--input", "-"],
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
        "number": value.get("number"),
        "html_url": value.get("html_url"),
        "draft": True,
    }


def _stale_reason(stored: GitHubIssueContext, fresh: GitHubIssueContext) -> str | None:
    if stored.issue_fingerprint != fresh.issue_fingerprint:
        return "Issue metadata/discussion changed since preparation"
    if stored.base_branch != fresh.base_branch or stored.base_sha != fresh.base_sha:
        return "repository base branch changed since preparation"
    return None


async def publish_issue_draft(
    task_id: str,
    *,
    workspace_path: str | Path = ".",
    approved: bool = False,
) -> dict[str, Any]:
    """Commit, push, and create a Draft PR only after explicit approval."""
    task_id = _validate_task(task_id)
    if not approved:
        return {
            "status": "waiting_approval",
            "data": {"task_id": task_id, "remote_side_effect": False},
            "evidence": [
                {
                    "kind": "github_issue_publish_not_approved",
                    "reason": "explicit approval is required",
                    "remote_side_effect": False,
                }
            ],
            "artifacts": [],
            "side_effect": False,
            "requires_approval": True,
        }
    root, stored, draft, final, extra = _load_prepared(workspace_path, task_id)
    if final.get("status") != "completed" or not final.get("acceptance_passed"):
        return {
            "status": "failed",
            "data": {
                "task_id": task_id,
                "pr_ready": False,
                "reason": "verified acceptance is required before publication",
                "remote_side_effect": False,
            },
            "evidence": [{"kind": "issue_patch_not_verified", "remote_side_effect": False}],
            "artifacts": [],
            "side_effect": False,
            "requires_approval": False,
        }
    fresh = await asyncio.to_thread(
        fetch_issue_context,
        stored.repo,
        stored.number,
        workspace_path=root,
        base_branch=stored.base_branch,
    )
    stale = _stale_reason(stored, fresh)
    if stale:
        return {
            "status": "failed",
            "data": {
                "task_id": task_id,
                "stale": True,
                "reason": stale,
                "remote_side_effect": False,
            },
            "evidence": [
                {
                    "kind": "stale_issue_or_base",
                    "stored_base_sha": stored.base_sha,
                    "current_base_sha": fresh.base_sha,
                    "remote_side_effect": False,
                }
            ],
            "artifacts": [],
            "side_effect": False,
            "requires_approval": False,
        }
    worktree = Path(extra["worktree"])
    if not worktree.is_dir():
        raise GitHubIssueWorkflowError(f"prepared worktree does not exist: {worktree}")
    branch, commit = _commit_patch(worktree, stored.title)
    payload = {
        "title": str(draft.get("title") or f"fix: {stored.title}")[:200],
        "body": str(draft.get("body") or f"Fixes {stored.issue_url}")[:60_000],
        "head": branch,
        "base": stored.base_branch,
        "draft": True,
    }
    from runtime.execution.durable import DurableExecutionRepository
    from runtime.execution.side_effects import SideEffectLedger

    repository = DurableExecutionRepository(
        sqlite_path=root / ".veya" / "execution-runtime.sqlite3"
    )
    await repository.connect()
    ledger = SideEffectLedger(repository)
    push_key = f"veya:github-issue-push:{stored.repo}:{stored.number}:{branch}:{commit}"
    draft_key = f"veya:github-pr-draft:{stored.repo}:{stored.number}:{branch}:{commit}"
    try:
        pushed = await ledger.execute(
            goal_run_id=f"issue-fix:{task_id}",
            work_item_id=f"push:{task_id}",
            operation_key=push_key,
            operation_type="git.push.issue_fix_branch",
            target_ref=f"github:{stored.repo}:{branch}",
            request={"repo": stored.repo, "branch": branch, "commit": commit},
            capability="manual_only",
            provider=lambda: asyncio.to_thread(_push_remote, worktree, branch),
        )
        created = await ledger.execute(
            goal_run_id=f"issue-fix:{task_id}",
            work_item_id=f"create-draft:{task_id}",
            operation_key=draft_key,
            operation_type="github.pull_request.create_draft",
            target_ref=f"github:{stored.repo}#{stored.number}",
            request=payload,
            capability="manual_only",
            provider=lambda: asyncio.to_thread(_create_draft_remote, stored, payload),
        )
    except Exception as exc:
        return {
            "status": "failed",
            "data": {
                "task_id": task_id,
                "branch": branch,
                "commit": commit,
                "remote_side_effect": "unknown",
            },
            "evidence": [
                {
                    "kind": "github_issue_publish_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "push_ledger_operation_key": push_key,
                    "draft_ledger_operation_key": draft_key,
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
            "repo": stored.repo,
            "issue_number": stored.number,
            "branch": branch,
            "commit": commit,
            "push": pushed,
            "draft_pr": created,
            "push_ledger_operation_key": push_key,
            "draft_ledger_operation_key": draft_key,
            "remote_side_effect": True,
        },
        "evidence": [
            {
                "kind": "github_issue_draft_pr_created",
                "repo": stored.repo,
                "issue_number": stored.number,
                "branch": branch,
                "commit": commit,
                "push_ledger_operation_key": push_key,
                "draft_ledger_operation_key": draft_key,
                "remote_side_effect": True,
            }
        ],
        "artifacts": [],
        "side_effect": True,
        "requires_approval": False,
    }


__all__ = [
    "GitHubIssueContext",
    "GitHubIssueWorkflowError",
    "fetch_issue_context",
    "prepare_issue_fix",
    "publish_issue_draft",
]
