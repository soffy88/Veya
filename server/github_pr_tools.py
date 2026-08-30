"""MasterAgent tools for the GitHub pull-request review product surface."""

from __future__ import annotations

from typing import Any

from runtime.github_pr import (
    GitHubPRReviewError,
    fetch_pr_context,
    post_pr_review,
    prepare_pr_review,
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


def github_pr_fetch(repo: str, pr_number: int) -> dict[str, Any]:
    """Read PR metadata, diff, comments, and review history without publishing."""
    try:
        context = fetch_pr_context(repo, pr_number)
    except Exception as exc:
        return _failed(f"GitHub PR fetch failed: {type(exc).__name__}: {exc}")
    return {
        "status": "ok",
        "data": {"context": context.to_dict()},
        "evidence": [
            {
                "kind": "github_pr_context_fetched",
                "repo": context.repo,
                "pr_number": context.number,
                "head_sha": context.head_sha,
                "changed_file_count": len(context.changed_files),
                "remote_side_effect": False,
            }
        ],
        "artifacts": [],
        "side_effect": False,
        "requires_approval": False,
    }


def github_pr_review_prepare(
    repo: str,
    pr_number: int,
    workspace_path: str = ".",
    task_id: str | None = None,
    profile: str = "local_restricted",
    run_verification: bool = True,
) -> dict[str, Any]:
    """Prepare an isolated, verified, non-published draft review."""
    try:
        return prepare_pr_review(
            repo,
            pr_number,
            workspace_path=workspace_path,
            task_id=task_id,
            profile=profile,
            run_verification=run_verification,
        )
    except Exception as exc:
        return _failed(f"GitHub PR review preparation failed: {type(exc).__name__}: {exc}")


async def github_pr_post_review(
    task_id: str,
    workspace_path: str = ".",
    event: str = "comment",
    approved: bool = False,
    body: str | None = None,
) -> dict[str, Any]:
    """Publish a prepared review only after explicit approval."""
    try:
        return await post_pr_review(
            task_id,
            workspace_path=workspace_path,
            event=event,  # type: ignore[arg-type]
            approved=approved,
            body=body,
        )
    except GitHubPRReviewError as exc:
        return _failed(f"GitHub PR review post rejected: {exc}")
    except Exception as exc:
        return _failed(f"GitHub PR review post failed: {type(exc).__name__}: {exc}")


def register_tools(registry: Any) -> int:
    """Register PR-08 tools into the existing MasterToolRegistry."""
    from server.tool_registry import SideEffect

    tools = [
        (
            "github_pr_fetch",
            "只读获取 GitHub PR metadata、base/head SHA、changed files、完整 diff、comments 和 review history；不发布任何内容。",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "GitHub owner/name。"},
                    "pr_number": {"type": "integer", "minimum": 1},
                },
                "required": ["repo", "pr_number"],
            },
            github_pr_fetch,
            SideEffect.PURE_READ,
        ),
        (
            "github_pr_review_prepare",
            "读取 PR 后创建 isolated worktree、checkout PR head、读取相关代码上下文并运行 required test/lint/typecheck sensors；只生成 draft ReviewArtifact，不发布。",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "pr_number": {"type": "integer", "minimum": 1},
                    "workspace_path": {"type": "string", "description": "本地 Git workspace。"},
                    "task_id": {"type": "string"},
                    "profile": {"type": "string"},
                    "run_verification": {"type": "boolean"},
                },
                "required": ["repo", "pr_number"],
            },
            github_pr_review_prepare,
            SideEffect.LOCAL_WRITE,
        ),
        (
            "github_pr_post_review",
            "发布已准备的 PR review（comment/approve/request_changes）；没有 approved=true 时绝不产生 GitHub 写入，且会先校验 head SHA 未过期。",
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "workspace_path": {"type": "string"},
                    "event": {"type": "string", "enum": ["comment", "approve", "request_changes"]},
                    "approved": {"type": "boolean"},
                    "body": {"type": "string"},
                },
                "required": ["task_id", "event", "approved"],
            },
            github_pr_post_review,
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
            parallel_safe=name == "github_pr_fetch",
            side_effect=side_effect,
            effect_capability="manual_only" if side_effect is not SideEffect.PURE_READ else "none",
        )
        added += 1
    return added


__all__ = [
    "github_pr_fetch",
    "github_pr_post_review",
    "github_pr_review_prepare",
    "register_tools",
]
