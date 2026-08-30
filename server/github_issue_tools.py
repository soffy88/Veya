"""MasterAgent tools for the GitHub Issue to verified patch workflow."""

from __future__ import annotations

from typing import Any

from runtime.github_issue import (
    GitHubIssueWorkflowError,
    fetch_issue_context,
    prepare_issue_fix,
    publish_issue_draft,
)


def _failed(message: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "data": {},
        "evidence": [{"kind": "error", "message": message}],
        "artifacts": [],
        "side_effect": False,
        "requires_approval": False,
    }


def github_issue_fetch(
    repo: str,
    issue_number: int,
    workspace_path: str = ".",
    base_branch: str | None = None,
) -> dict[str, Any]:
    """Read Issue metadata and repository context without remote mutation."""
    try:
        context = fetch_issue_context(
            repo,
            issue_number,
            workspace_path=workspace_path,
            base_branch=base_branch,
        )
    except Exception as exc:
        return _failed(f"GitHub Issue fetch failed: {type(exc).__name__}: {exc}")
    return {
        "status": "ok",
        "data": {"context": context.to_dict()},
        "evidence": [
            {
                "kind": "github_issue_context_fetched",
                "repo": context.repo,
                "issue_number": context.number,
                "base_branch": context.base_branch,
                "base_sha": context.base_sha,
                "remote_side_effect": False,
            }
        ],
        "artifacts": [],
        "side_effect": False,
        "requires_approval": False,
    }


async def github_issue_fix_prepare(
    repo: str,
    issue_number: int,
    patch: str,
    workspace_path: str = ".",
    task_id: str | None = None,
    base_branch: str | None = None,
    profile: str = "local_restricted",
    run_verification: bool = True,
) -> dict[str, Any]:
    """Apply a MasterAgent-provided patch in isolation and verify it locally."""
    try:
        return await prepare_issue_fix(
            repo,
            issue_number,
            patch,
            workspace_path=workspace_path,
            task_id=task_id,
            base_branch=base_branch,
            profile=profile,
            run_verification=run_verification,
        )
    except GitHubIssueWorkflowError as exc:
        return _failed(f"GitHub Issue fix preparation rejected: {exc}")
    except Exception as exc:
        return _failed(f"GitHub Issue fix preparation failed: {type(exc).__name__}: {exc}")


async def github_pr_create_draft(
    task_id: str,
    workspace_path: str = ".",
    approved: bool = False,
) -> dict[str, Any]:
    """Push a verified Issue branch and create a Draft PR after explicit approval."""
    try:
        return await publish_issue_draft(
            task_id,
            workspace_path=workspace_path,
            approved=approved,
        )
    except GitHubIssueWorkflowError as exc:
        return _failed(f"GitHub Draft PR creation rejected: {exc}")
    except Exception as exc:
        return _failed(f"GitHub Draft PR creation failed: {type(exc).__name__}: {exc}")


def register_tools(registry: Any) -> int:
    """Register Issue workflow tools in the existing MasterToolRegistry."""
    from server.tool_registry import SideEffect

    tools = [
        (
            "github_issue_fetch",
            "只读获取 GitHub Issue metadata、labels、comments、referenced files、linked PRs 和 repository base SHA；不修改远端。",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "GitHub owner/name。"},
                    "issue_number": {"type": "integer", "minimum": 1},
                    "workspace_path": {"type": "string"},
                    "base_branch": {"type": "string"},
                },
                "required": ["repo", "issue_number"],
            },
            github_issue_fetch,
            SideEffect.PURE_READ,
        ),
        (
            "github_issue_fix_prepare",
            "获取 Issue，在 isolated worktree 中应用 MasterAgent 提供的 unified patch，运行 required sensors，并生成 PRDraftArtifact；不 commit/push/create PR。",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "issue_number": {"type": "integer", "minimum": 1},
                    "patch": {
                        "type": "string",
                        "description": "由 MasterAgent 生成的 unified diff。",
                    },
                    "workspace_path": {"type": "string"},
                    "task_id": {"type": "string"},
                    "base_branch": {"type": "string"},
                    "profile": {"type": "string"},
                    "run_verification": {"type": "boolean"},
                },
                "required": ["repo", "issue_number", "patch"],
            },
            github_issue_fix_prepare,
            SideEffect.LOCAL_WRITE,
        ),
        (
            "github_pr_create_draft",
            "在 verified Issue patch 且显式 approved=true 后，使用 SideEffectLedger push branch 并创建 Draft PR；未批准、stale 或验证失败均不产生远端写入。",
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "workspace_path": {"type": "string"},
                    "approved": {"type": "boolean"},
                },
                "required": ["task_id", "approved"],
            },
            github_pr_create_draft,
            SideEffect.EXTERNAL_MUTATION,
        ),
    ]
    added = 0
    for name, description, parameters, function, side_effect in tools:
        if registry.has(name):
            continue
        registry.register(
            name,
            description,
            parameters,
            function,
            max_result_chars=50000,
            parallel_safe=name == "github_issue_fetch",
            side_effect=side_effect,
            effect_capability="manual_only" if side_effect is not SideEffect.PURE_READ else "none",
        )
        added += 1
    return added


__all__ = [
    "github_issue_fetch",
    "github_issue_fix_prepare",
    "github_pr_create_draft",
    "register_tools",
]
