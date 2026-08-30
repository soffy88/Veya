"""CLI for the MasterAgent-backed GitHub pull-request review flow."""

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
    parser = argparse.ArgumentParser(prog="veya gh pr", description="Review a GitHub pull request")
    sub = parser.add_subparsers(dest="action", required=True)

    inspect_parser = sub.add_parser("inspect", help="fetch PR context without local writes")
    inspect_parser.add_argument("number", type=int)
    inspect_parser.add_argument("--repo", required=True)
    inspect_parser.add_argument("--json", action="store_true", dest="json_output")

    review_parser = sub.add_parser("review", help="prepare an isolated draft review")
    review_parser.add_argument("number", type=int)
    review_parser.add_argument("--repo", required=True)
    review_parser.add_argument("--path", default=".", help="local Git workspace")
    review_parser.add_argument("--profile", default="local_restricted")
    review_parser.add_argument("--no-verify", action="store_true")
    review_parser.add_argument("--json", action="store_true", dest="json_output")

    post_parser = sub.add_parser("post", help="post a prepared review after explicit approval")
    post_parser.add_argument("task_id")
    post_parser.add_argument("--path", default=".", help="workspace containing .veya/runs")
    post_parser.add_argument(
        "--event",
        choices=("comment", "approve", "request_changes"),
        help="explicit GitHub review event",
    )
    post_parser.add_argument(
        "--approve",
        action="store_true",
        help="explicitly authorize the remote review mutation",
    )
    post_parser.add_argument("--json", action="store_true", dest="json_output")
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
    status = value.get("status", "unknown")
    print(f"GitHub PR review: {status}")
    data = value.get("data") or {}
    artifact = data.get("review_artifact")
    if isinstance(artifact, dict):
        print(f"Task: {artifact.get('task_id', '?')}")
        print(f"Verdict: {artifact.get('verdict', '?')}")
        print(f"Comments: {len(artifact.get('comments') or [])}")
        print("Publication: not performed")
    context = data.get("context")
    if isinstance(context, dict):
        print(f"PR: {context.get('repo')}#{context.get('number')} {context.get('title', '')}")
        print(f"Head: {context.get('head_sha')}")
    evidence = value.get("evidence") or []
    if evidence and isinstance(evidence[0], dict) and evidence[0].get("reason"):
        print(f"Reason: {evidence[0]['reason']}")


async def _post(task_id: str, workspace_path: str, event: str, *, json_output: bool) -> int:
    from server.coordinator_master import master_coordinator

    result = await master_coordinator.handle_tool_call(
        "github_pr_post_review",
        {
            "task_id": task_id,
            "workspace_path": str(Path(workspace_path).expanduser().resolve()),
            "event": event,
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


def run_github_pr_cli(argv: list[str] | None = None) -> int:
    """Run `veya gh pr ...`; all review preparation goes through MasterAgent."""
    args = _parser().parse_args(argv)
    if args.action == "inspect":
        value = asyncio.run(
            _master_request(
                f"[GITHUB PR INSPECT]\nFetch read-only context for PR #{args.number} in {args.repo}. "
                "Use github_pr_fetch and return its structured result; do not publish.",
                tool_name="github_pr_fetch",
            )
        )
        _print(value, json_output=args.json_output)
        return EXIT_OK if value.get("status") == "ok" else EXIT_FAILED
    if args.action == "review":
        value = asyncio.run(
            _master_request(
                "[GITHUB PR REVIEW]\n"
                f"Prepare a review for PR #{args.number} in {args.repo}.\n"
                f"Local workspace: {Path(args.path).expanduser().resolve()}\n"
                f"Verification profile: {args.profile}\n"
                f"Run required verification: {not args.no_verify}\n"
                "Use github_pr_review_prepare. Analyze the returned evidence, keep the review draft, "
                "and never call github_pr_post_review.",
                tool_name="github_pr_review_prepare",
            )
        )
        _print(value, json_output=args.json_output)
        return EXIT_OK if value.get("status") == "ok" else EXIT_FAILED

    event = args.event
    explicitly_approved = bool(args.approve)
    if not explicitly_approved:
        if not sys.stdin.isatty():
            value = {
                "status": "waiting_approval",
                "data": {"task_id": args.task_id, "remote_side_effect": False},
                "evidence": [
                    {
                        "kind": "explicit_approval_required",
                        "message": "re-run interactively and type POST, or pass --approve explicitly",
                        "remote_side_effect": False,
                    }
                ],
            }
            _print(value, json_output=args.json_output)
            return EXIT_WAITING_APPROVAL
        try:
            explicitly_approved = (
                input(
                    "This will publish to GitHub. Type POST to approve the remote side effect: "
                ).strip()
                == "POST"
            )
        except (EOFError, KeyboardInterrupt):
            explicitly_approved = False
    if not explicitly_approved:
        value = {
            "status": "waiting_approval",
            "data": {"task_id": args.task_id, "remote_side_effect": False},
            "evidence": [{"kind": "explicit_approval_denied", "remote_side_effect": False}],
        }
        _print(value, json_output=args.json_output)
        return EXIT_WAITING_APPROVAL
    if event is None:
        if not sys.stdin.isatty():
            print("--event is required with --approve in non-interactive mode", file=sys.stderr)
            return EXIT_WAITING_APPROVAL
        event = input("Event [comment/approve/request_changes]: ").strip()
    if event not in {"comment", "approve", "request_changes"}:
        print("invalid review event", file=sys.stderr)
        return EXIT_FAILED
    return asyncio.run(_post(args.task_id, args.path, event, json_output=args.json_output))


__all__ = ["run_github_pr_cli"]
