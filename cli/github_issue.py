"""CLI for the MasterAgent-backed GitHub Issue fix workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_WAITING_APPROVAL = 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="veya gh issue", description="Prepare a verified patch from a GitHub Issue"
    )
    sub = parser.add_subparsers(dest="action", required=True)

    inspect_parser = sub.add_parser("inspect", help="fetch Issue context without local writes")
    inspect_parser.add_argument("number", type=int)
    inspect_parser.add_argument("--repo", required=True)
    inspect_parser.add_argument("--path", default=".", help="local Git workspace")
    inspect_parser.add_argument("--base-branch")
    inspect_parser.add_argument("--json", action="store_true", dest="json_output")

    fix_parser = sub.add_parser("fix", help="prepare and verify an Issue patch")
    fix_parser.add_argument("number", type=int)
    fix_parser.add_argument("--repo", required=True)
    fix_parser.add_argument("--path", default=".", help="local Git workspace")
    fix_parser.add_argument("--base-branch")
    fix_parser.add_argument("--profile", default="local_restricted")
    fix_parser.add_argument("--json", action="store_true", dest="json_output")

    publish_parser = sub.add_parser(
        "publish", help="push a verified Issue branch and create a Draft PR after approval"
    )
    publish_parser.add_argument("task_id")
    publish_parser.add_argument("--path", default=".", help="workspace containing .veya/runs")
    publish_parser.add_argument(
        "--approve", action="store_true", help="explicitly authorize push and Draft PR creation"
    )
    publish_parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def _tool_result(result: dict[str, Any], *, name: str) -> dict[str, Any] | None:
    for item in result.get("tool_calls", []):
        if item.get("tool") != name:
            continue
        raw = item.get("result")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                return {"status": "failed", "error": raw}
            return value if isinstance(value, dict) else {"status": "failed", "error": str(value)}
    return None


async def _master_request(prompt: str, *, tool_name: str) -> dict[str, Any]:
    from server.coordinator_master import master_coordinator

    result = await master_coordinator.chat_stream(prompt, session_id=None)
    extracted = _tool_result(result, name=tool_name)
    if extracted is not None:
        return extracted
    return {
        "status": "failed",
        "data": {},
        "evidence": [
            {
                "kind": "master_agent_tool_not_called",
                "expected_tool": tool_name,
                "final_answer": str(result.get("final_answer") or "")[:1000],
            }
        ],
    }


def _print(value: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    print(f"GitHub Issue workflow: {value.get('status', 'unknown')}")
    data = value.get("data") or {}
    context = data.get("context")
    if isinstance(context, dict):
        print(f"Issue: {context.get('repo')}#{context.get('number')} {context.get('title', '')}")
        print(f"Base: {context.get('base_branch')} @ {context.get('base_sha')}")
    draft = data.get("pr_draft")
    if isinstance(draft, dict):
        print(f"Draft status: {draft.get('status')}")
        print(f"Branch: {draft.get('head_branch')}")
        print(f"Files: {', '.join(draft.get('files_changed') or []) or '(none)'}")
    final = data.get("final_result")
    if isinstance(final, dict):
        print(f"Acceptance: {final.get('acceptance_passed')}")
        print(f"Task: {final.get('task_id')}")
    if value.get("status") == "waiting_approval":
        print("Publication: not performed; explicit approval is required")


async def _publish(task_id: str, workspace_path: str, *, json_output: bool) -> int:
    from server.coordinator_master import master_coordinator

    result = await master_coordinator.handle_tool_call(
        "github_pr_create_draft",
        {
            "task_id": task_id,
            "workspace_path": str(Path(workspace_path).expanduser().resolve()),
            "approved": True,
        },
    )
    try:
        value = json.loads(result)
    except json.JSONDecodeError:
        value = {"status": "failed", "error": result}
    if not isinstance(value, dict):
        value = {"status": "failed", "error": str(value)}
    _print(value, json_output=json_output)
    return EXIT_OK if value.get("status") == "ok" else EXIT_FAILED


def run_github_issue_cli(argv: list[str] | None = None) -> int:
    """Run `veya gh issue ...`; reads and preparation stay on MasterAgent."""
    args = _parser().parse_args(argv)
    if args.action == "inspect":
        value = asyncio.run(
            _master_request(
                "[GITHUB ISSUE INSPECT]\n"
                f"Fetch read-only context for Issue #{args.number} in {args.repo}. "
                f"Local workspace: {Path(args.path).expanduser().resolve()}\n"
                f"Base branch: {args.base_branch or '(repository default)'}\n"
                "Use github_issue_fetch and return its structured result; do not publish.",
                tool_name="github_issue_fetch",
            )
        )
        _print(value, json_output=args.json_output)
        return EXIT_OK if value.get("status") == "ok" else EXIT_FAILED
    if args.action == "fix":
        value = asyncio.run(
            _master_request(
                "[GITHUB ISSUE FIX]\n"
                f"Prepare a verified patch for Issue #{args.number} in {args.repo}.\n"
                f"Local workspace: {Path(args.path).expanduser().resolve()}\n"
                f"Base branch: {args.base_branch or '(repository default)'}\n"
                f"Verification profile: {args.profile}\n"
                "First use github_issue_fetch, inspect the relevant code, and use the existing "
                "coding worktree/patch tools as needed. Compose a unified diff and then call "
                "github_issue_fix_prepare with that patch. Do not commit, push, create a PR, "
                "comment, close, or change labels.",
                tool_name="github_issue_fix_prepare",
            )
        )
        _print(value, json_output=args.json_output)
        return EXIT_OK if value.get("status") in {"ok", "partial"} else EXIT_FAILED

    approved = bool(args.approve)
    if not approved:
        if not sys.stdin.isatty():
            value = {
                "status": "waiting_approval",
                "data": {"task_id": args.task_id, "remote_side_effect": False},
                "evidence": [
                    {
                        "kind": "explicit_approval_required",
                        "message": "re-run interactively and type PUBLISH, or pass --approve explicitly",
                        "remote_side_effect": False,
                    }
                ],
            }
            _print(value, json_output=args.json_output)
            return EXIT_WAITING_APPROVAL
        try:
            approved = (
                input(
                    "This will push a branch and create a Draft PR. Type PUBLISH to approve: "
                ).strip()
                == "PUBLISH"
            )
        except (EOFError, KeyboardInterrupt):
            approved = False
    if not approved:
        value = {
            "status": "waiting_approval",
            "data": {"task_id": args.task_id, "remote_side_effect": False},
            "evidence": [{"kind": "explicit_approval_denied", "remote_side_effect": False}],
        }
        _print(value, json_output=args.json_output)
        return EXIT_WAITING_APPROVAL
    return asyncio.run(_publish(args.task_id, args.path, json_output=args.json_output))


__all__ = ["run_github_issue_cli"]
