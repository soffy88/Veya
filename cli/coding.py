"""CLI `veya code` command — MasterAgent-backed coding workflow.

This module implements the `veya code` CLI that routes through MasterAgent,
NOT directly to CodingTaskService.

Key constraint:
- `veya code "<objective>"` MUST go through MasterAgent → coding_task_run tool
- Read-only commands (--status, --diff, --artifacts) can read task store directly
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Exit codes (documented and stable)
EXIT_COMPLETED = 0
EXIT_PARTIAL = 2
EXIT_FAILED = 3
EXIT_WAITING_APPROVAL = 4
EXIT_CANCELLED = 5
EXIT_HARNESS_BLOCKED = 6
EXIT_INVALID_WORKSPACE = 7


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="veya code",
        description="Execute a coding task through MasterAgent with full harness integration.",
    )
    p.add_argument("objective", nargs="?", help="The coding task objective")
    p.add_argument("--path", default=".", help="Workspace path (default: current directory)")
    p.add_argument("--json", action="store_true", help="Output result as JSON")
    p.add_argument("--max-wall", type=int, help="Maximum wall-clock time in seconds")
    p.add_argument(
        "--continue", dest="continue_task", metavar="TASK_ID", help="Resume a previous task"
    )
    p.add_argument("--status", metavar="TASK_ID", help="Show task status")
    p.add_argument("--diff", metavar="TASK_ID", help="Show task diff")
    p.add_argument("--artifacts", metavar="TASK_ID", help="List task artifacts")
    p.add_argument("--verbose", action="store_true", help="Verbose output")
    return p


def _resolve_workspace(path: str) -> Path:
    """Resolve and validate workspace path."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        print(f"Error: Path does not exist: {path}", file=sys.stderr)
        sys.exit(EXIT_INVALID_WORKSPACE)
    if not (p / ".git").exists():
        print(f"Error: Not a Git repository: {path}", file=sys.stderr)
        sys.exit(EXIT_INVALID_WORKSPACE)
    return p


def _status_to_exit_code(status: str) -> int:
    """Map task status to exit code."""
    mapping = {
        "completed": EXIT_COMPLETED,
        "partial_completed": EXIT_PARTIAL,
        "failed": EXIT_FAILED,
        "waiting_approval": EXIT_WAITING_APPROVAL,
        "cancelled": EXIT_CANCELLED,
    }
    return mapping.get(status, EXIT_FAILED)


def _print_task_result(result: dict[str, Any], *, json_output: bool = False) -> None:
    """Print task result in human-readable or JSON format."""
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    status = result.get("status", "unknown")
    task_id = result.get("task_id", "?")
    acceptance = result.get("acceptance_passed", False)

    # Status line
    status_emoji = {
        "completed": "✅",
        "partial_completed": "⚠️",
        "failed": "❌",
        "cancelled": "⏹️",
        "waiting_approval": "⏳",
    }.get(status, "❓")

    print(f"\n{status_emoji} Task {task_id}: {status}")
    print(f"   Acceptance: {'PASSED' if acceptance else 'FAILED'}")

    # Changed files
    changed = result.get("changed_files", [])
    if changed:
        print(f"\n   Changed files ({len(changed)}):")
        for f in changed[:10]:
            print(f"     - {f}")
        if len(changed) > 10:
            print(f"     ... and {len(changed) - 10} more")

    # Summary
    summary = result.get("final_summary", "")
    if summary:
        print(f"\n   Summary: {summary[:500]}")

    # Artifacts
    artifacts = result.get("artifact_ids", [])
    if artifacts:
        print(f"\n   Artifacts: {len(artifacts)}")

    # Verification report
    vr_id = result.get("verification_report_id")
    if vr_id:
        print(f"   Verification: {vr_id}")


async def _run_coding_task(
    workspace_path: Path,
    objective: str,
    *,
    resume_task_id: str | None = None,
    max_wall_seconds: int | None = None,
    json_output: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """Run a coding task through MasterAgent.

    This MUST go through MasterAgent.chat_stream, NOT directly to CodingTaskService.
    """
    from server.coordinator_master import master_coordinator

    # Build the prompt that will trigger coding_task_run tool
    # MasterAgent will understand this and call the appropriate tool
    prompt_parts = ["[CODE TASK]", f"Workspace: {workspace_path}", f"Objective: {objective}"]

    if resume_task_id:
        prompt_parts.append(f"Resume task: {resume_task_id}")
    if max_wall_seconds:
        prompt_parts.append(f"Max wall time: {max_wall_seconds}s")

    prompt = "\n".join(prompt_parts)

    if verbose:
        print("Sending to MasterAgent...", file=sys.stderr)
        print(f"  Workspace: {workspace_path}", file=sys.stderr)
        print(f"  Objective: {objective[:100]}...", file=sys.stderr)

    # Call MasterAgent
    result = await master_coordinator.chat_stream(
        prompt,
        session_id=None,  # New session for each coding task
    )

    # Extract coding task result from tool calls
    tool_calls = result.get("tool_calls", [])
    coding_result = None
    _called_coding_task = False

    for tc in tool_calls:
        if tc.get("tool") == "coding_task_run":
            _called_coding_task = True
            # Parse the tool result
            tool_result = tc.get("result", "")
            if not tool_result:
                # MasterAgent 的工具调用 trace 只有 {tool, status}, 不带结果
                # payload; 真值在 durable CodingTask 存储里, 见下方兜底。
                continue
            try:
                coding_result = (
                    json.loads(tool_result) if isinstance(tool_result, str) else tool_result
                )
            except json.JSONDecodeError:
                coding_result = {"status": "failed", "error": "Invalid tool result"}

    if _called_coding_task and not coding_result:
        # 真实主链: MasterAgent 已调用 coding_task_run 且任务已 durable 落盘,
        # 但返回的 tool_calls 不含结果 → 从 CodingTaskService (唯一持久权威)
        # 读最近一次任务的结果, 而不是把主链行为降级为失败。
        try:
            from runtime.coding.task_service import CodingTaskService

            service = CodingTaskService(str(workspace_path))
            tasks = service.list_tasks()
            if tasks:
                latest = tasks[0]
                final_result = latest.final_result or {}
                coding_result = {
                    "task_id": latest.task_id,
                    "goal_run_id": latest.goal_run_id,
                    "status": final_result.get("status") or latest.status,
                    "verification_report_id": final_result.get("verification_report_id"),
                    "artifact_ids": final_result.get("artifact_ids", []),
                    "changed_files": final_result.get("changed_files", []),
                    "final_summary": final_result.get("final_summary") or latest.objective,
                    "acceptance_passed": bool(final_result.get("acceptance_passed")),
                }
        except Exception as exc:
            coding_result = {
                "status": "failed",
                "error": f"coding_task_run 已调用但结果读取失败: {exc}",
                "acceptance_passed": False,
            }

    if not coding_result:
        # MasterAgent didn't call coding_task_run, return error
        final_answer = result.get("final_answer", "")
        coding_result = {
            "status": "failed",
            "error": f"MasterAgent did not execute coding task. Response: {final_answer[:500]}",
            "acceptance_passed": False,
        }

    return coding_result


async def _show_task_status(project_root: Path, task_id: str, *, json_output: bool = False) -> int:
    """Show task status (read-only, can read task store directly)."""
    from runtime.coding.task_service import CodingTaskService

    service = CodingTaskService(str(project_root))
    state = service.get_task_state(task_id)

    if not state:
        print(f"Task not found: {task_id}", file=sys.stderr)
        return EXIT_FAILED

    if json_output:
        print(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(f"Task: {state.task_id}")
    print(f"Status: {state.status}")
    print(f"Objective: {state.objective[:100]}")
    print(f"Created: {state.created_at}")
    if state.completed_at:
        print(f"Completed: {state.completed_at}")
    if state.worktree_path:
        print(f"Worktree: {state.worktree_path}")
    if state.goal_run_id:
        print(f"GoalRun: {state.goal_run_id}")
    if state.error:
        print(f"Error: {state.error}")
    return 0


async def _show_task_diff(project_root: Path, task_id: str, *, json_output: bool = False) -> int:
    """Show task diff (read-only)."""
    from runtime.coding.task_service import CodingTaskService

    service = CodingTaskService(str(project_root))
    diff = service.get_task_diff(task_id)

    if not diff:
        print(f"No diff available for task: {task_id}", file=sys.stderr)
        return EXIT_FAILED

    if json_output:
        print(json.dumps(diff, ensure_ascii=False, indent=2))
        return 0

    # Print human-readable diff
    print(f"Diff for task: {task_id}")
    print("-" * 60)
    print(diff.get("diff", "(no changes)"))
    return 0


async def _show_task_artifacts(
    project_root: Path, task_id: str, *, json_output: bool = False
) -> int:
    """List task artifacts (read-only)."""
    from runtime.coding.task_service import CodingTaskService

    service = CodingTaskService(str(project_root))
    artifacts = service.get_task_artifacts(task_id)

    if not artifacts:
        print(f"No artifacts for task: {task_id}", file=sys.stderr)
        return 0

    if json_output:
        print(json.dumps(artifacts, ensure_ascii=False, indent=2))
        return 0

    print(f"Artifacts for task: {task_id}")
    print("-" * 60)
    for art in artifacts:
        name = art.get("name", "?")
        size = art.get("size", 0)
        print(f"  {name} ({size} bytes)")
    return 0


async def _continue_task(
    project_root: Path,
    task_id: str,
    *,
    json_output: bool = False,
    verbose: bool = False,
) -> int:
    """Continue a previous task through MasterAgent."""
    from runtime.coding.task_service import CodingTaskService

    service = CodingTaskService(str(project_root))
    state = service.get_task_state(task_id)

    if not state:
        print(f"Task not found: {task_id}", file=sys.stderr)
        return EXIT_FAILED

    if state.status in ("completed", "cancelled"):
        print(f"Task already {state.status}", file=sys.stderr)
        _print_task_result(state.final_result or {}, json_output=json_output)
        return _status_to_exit_code(state.status)

    # Resume through MasterAgent
    result = await _run_coding_task(
        project_root,
        state.objective,
        resume_task_id=task_id,
        json_output=json_output,
        verbose=verbose,
    )

    _print_task_result(result, json_output=json_output)
    return _status_to_exit_code(result.get("status", "failed"))


def run_coding_cli(argv: list[str] | None = None) -> int:
    """Main entry point for `veya code` CLI."""
    args = _build_parser().parse_args(argv)

    # Handle read-only commands first
    if args.status:
        project_root = _resolve_workspace(args.path)
        return asyncio.run(_show_task_status(project_root, args.status, json_output=args.json))

    if args.diff:
        project_root = _resolve_workspace(args.path)
        return asyncio.run(_show_task_diff(project_root, args.diff, json_output=args.json))

    if args.artifacts:
        project_root = _resolve_workspace(args.path)
        return asyncio.run(
            _show_task_artifacts(project_root, args.artifacts, json_output=args.json)
        )

    # Handle --continue
    if args.continue_task:
        project_root = _resolve_workspace(args.path)
        return asyncio.run(
            _continue_task(
                project_root,
                args.continue_task,
                json_output=args.json,
                verbose=args.verbose,
            )
        )

    # Require objective for new task
    if not args.objective:
        print("Error: objective required for new coding task", file=sys.stderr)
        print('Usage: veya code "<objective>"', file=sys.stderr)
        return 2

    # Run new coding task through MasterAgent
    project_root = _resolve_workspace(args.path)

    result = asyncio.run(
        _run_coding_task(
            project_root,
            args.objective,
            max_wall_seconds=args.max_wall,
            json_output=args.json,
            verbose=args.verbose,
        )
    )

    _print_task_result(result, json_output=args.json)
    return _status_to_exit_code(result.get("status", "failed"))


__all__ = [
    "EXIT_CANCELLED",
    "EXIT_COMPLETED",
    "EXIT_FAILED",
    "EXIT_HARNESS_BLOCKED",
    "EXIT_INVALID_WORKSPACE",
    "EXIT_PARTIAL",
    "EXIT_WAITING_APPROVAL",
    "run_coding_cli",
]
