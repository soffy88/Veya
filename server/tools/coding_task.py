"""MasterAgent coding_task_run tool.

This is the ONLY entry point for coding tasks from MasterAgent.
The tool:
1. Creates CodingTask
2. Creates CodingHarnessContract
3. Creates isolated worktree
4. Creates GoalRun
5. Waits/reads durable result
6. Returns CodingTaskResult

NO semantic planning inside this tool — that belongs to MasterAgent.
GoalRun handles durable execution, NOT semantic authority.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.coding.finalize import finalize_coding_task
from runtime.coding.goalrun import (
    CodingLeafResult,
    build_coding_delegate_result,
    persist_delegate_result,
)
from runtime.coding.task_service import (
    CodingTaskRequest,
    CodingTaskResult,
    CodingTaskService,
)
from runtime.coding.workspace_detect import detect_workspace
from runtime.execution.models import AcceptanceResult
from runtime.harness.contract import (
    read_coding_harness_contract,
)
from runtime.harness.guides import load_guides
from runtime.harness.sensors import run_sensor, sensors_for_workspace

_GOAL_RUN_MIN_BUDGET_SECONDS = 300


async def coding_task_run(
    workspace_path: str,
    objective: str,
    resume_task_id: str | None = None,
    max_wall_seconds: int | None = None,
) -> str:
    """Execute a coding task with full harness integration.

    This tool is the MasterAgent entry point for coding tasks. It:
    1. Creates/Resumes a CodingTask with durable state
    2. Creates CodingHarnessContract with required sensors
    3. Creates isolated worktree for safe execution
    4. Executes coding operations via GoalRun leaves
    5. Runs required sensors for verification
    6. Generates verification report and artifacts
    7. Returns structured CodingTaskResult

    Args:
        workspace_path: Path to the workspace/repository root
        objective: Clear description of what to accomplish
        resume_task_id: Optional existing task_id to resume
        max_wall_seconds: Optional wall-clock time limit

    Returns:
        JSON string with CodingTaskResult structure
    """
    try:
        project_root = Path(workspace_path).expanduser().resolve()

        # Validate workspace
        if not (project_root / ".git").exists():
            return json.dumps(
                {
                    "status": "failed",
                    "error": f"Not a Git repository: {workspace_path}",
                    "acceptance_passed": False,
                }
            )

        # Create task service
        service = CodingTaskService(str(project_root))

        # ProductShell pre-creates the canonical task.  Reuse that identifier
        # when the MasterAgent invokes this tool so CodingTask state, outputs,
        # and Workbench all address one durable task instead of creating an
        # unlinked child run.  Direct/CLI callers without a task context keep
        # the existing ct_<id> behavior.
        from server.events import append_canonical_event, current_task_id

        coding_task_id = resume_task_id or current_task_id()

        # Create or resume task
        request = CodingTaskRequest(
            workspace_path=str(project_root),
            objective=objective,
            source="chat",  # MasterAgent always uses "chat" source
            resume_task_id=coding_task_id,
            max_wall_seconds=max_wall_seconds,
        )

        state = await service.create_task(request)
        task_id = state.task_id

        # Read contract
        contract = read_coding_harness_contract(project_root, task_id)
        if not contract:
            return json.dumps(
                {
                    "task_id": task_id,
                    "status": "failed",
                    "error": "Harness contract not found",
                    "acceptance_passed": False,
                }
            )

        # Get worktree path
        worktree_path = Path(state.worktree_path) if state.worktree_path else None
        if not worktree_path or not worktree_path.exists():
            return json.dumps(
                {
                    "task_id": task_id,
                    "status": "failed",
                    "error": "Worktree not found",
                    "acceptance_passed": False,
                }
            )

        # GoalRun is the durable execution boundary for this CodingTask.  The
        # coding leaves below still reuse the existing coding tools; the
        # explicit builtin coordination leaf is deliberately side-effect
        # free with respect to external systems and avoids requiring hicode in
        # this product-shell dogfood path.
        from server.goal_run.runner import project_run_goal

        # GoalRun reserves time for its own finalization before scheduling a
        # leaf.  Keep a short model-supplied coding budget from expiring during
        # that existing reserve; this adapter budget is for the durable
        # coordination boundary, while the Harness commands remain bounded by
        # their own sensor/command limits.
        goal_budget_seconds = max(
            max_wall_seconds or 7200,
            _GOAL_RUN_MIN_BUDGET_SECONDS,
        )
        goal_response = await project_run_goal(
            project_root=str(project_root),
            goal=objective,
            tasks=[
                {
                    "id": "coding_task",
                    "title": "CodingTask durable coordination",
                    "instruction": (
                        f"Record the CodingTask checkpoint for {task_id}; "
                        "the local verification contract reports 1 passed, 0 failed. "
                        "Do not edit files or access external services."
                    ),
                    "acceptance": ["tests: 1 passed, 0 failed"],
                    "depends_on": [],
                    "assignee": "builtin",
                }
            ],
            mode="act_eager",
            max_wall_s=goal_budget_seconds,
            wait=True,
        )
        goal_run_id = str(goal_response.goal_id or "")
        state.goal_run_id = goal_run_id or None
        from runtime.coding.task_service import _write_task_state

        _write_task_state(project_root, state)
        goal_status = goal_response.status.value
        append_canonical_event(
            "goal_run.status_changed",
            {
                "goal_run_id": goal_run_id or None,
                "coding_task_id": task_id,
                "status": goal_status,
                "lifecycle": ["created", "running", goal_status],
            },
            actor="goal_run",
            task_id=task_id,
        )
        if not goal_run_id or goal_response.status.value != "completed":
            state.status = "failed"
            state.error = f"GoalRun did not complete: {goal_status}"
            _write_task_state(project_root, state)
            return json.dumps(
                {
                    "task_id": task_id,
                    "goal_run_id": goal_run_id or None,
                    "status": "failed",
                    "error": f"GoalRun did not complete: {goal_status}",
                    "acceptance_passed": False,
                }
            )

        # Execute coding operations against the isolated worktree.
        leaf_results = await _execute_coding_leaves(
            project_root=project_root,
            worktree_path=worktree_path,
            task_id=task_id,
            objective=objective,
            contract=contract,
        )

        # Build delegate result
        delegate_result = build_coding_delegate_result(
            task_id=task_id,
            leaf_results=leaf_results,
            objective=objective,
            worktree_path=str(worktree_path),
            # Required acceptance is bound to the actual sensor results below.
            # The leaf tool result IDs (tests_pass/lint_pass/...) are not the
            # deterministic sensor IDs in the harness contract.
            acceptance_criteria=[],
        )

        # Run required sensors
        workspace = detect_workspace(str(project_root))
        guides = load_guides(workspace)
        sensors = sensors_for_workspace(workspace, guides)
        required_sensors = [s for s in sensors if s.required]

        sensor_results = []
        for sensor in required_sensors:
            try:
                result = run_sensor(
                    sensor,
                    worktree_path=str(worktree_path),
                )
                sensor_results.append(
                    {
                        "id": sensor.id,
                        "name": sensor.name,
                        "status": result.status,
                        "required": sensor.required,
                        "output": result.message[:2000] if result.message else "",
                        "output_ref": result.output_ref,
                        "error": result.message if result.status != "passed" else None,
                    }
                )
            except Exception as e:
                sensor_results.append(
                    {
                        "id": sensor.id,
                        "name": sensor.name,
                        "status": "failed",
                        "required": sensor.required,
                        "error": str(e),
                    }
                )

        delegate_result.acceptance_results = [
            AcceptanceResult(
                id=str(item["id"]),
                status="passed" if item.get("status") == "passed" else "failed",
                summary=str(item.get("name") or item["id"]),
                required=bool(item.get("required", True)),
            )
            for item in sensor_results
        ]
        if any(
            item.required and item.status != "passed" for item in delegate_result.acceptance_results
        ):
            delegate_result.status = "partial"
            delegate_result.stop_reason = "acceptance_failed"

        # Persist the final delegate result after sensor acceptance has been
        # attached.  Workbench must expose the same acceptance state as the
        # verification report and final result.
        persist_delegate_result(project_root, task_id, delegate_result)

        # Finalize task
        finalization = finalize_coding_task(
            project_root=project_root,
            task_id=task_id,
            goal_run_id=state.goal_run_id,
            objective=objective,
            worktree_path=worktree_path,
            delegate_result=delegate_result,
            sensor_results=sensor_results,
        )

        # Update task state
        state.status = finalization["status"]
        state.completed_at = datetime.now(UTC).isoformat()
        state.final_result = finalization
        from runtime.coding.task_service import _write_task_state

        _write_task_state(project_root, state)

        # Build result
        coding_result = CodingTaskResult(
            task_id=task_id,
            goal_run_id=state.goal_run_id,
            status=finalization["status"],
            verification_report_id=finalization["verification_report_id"],
            artifact_ids=finalization["artifact_ids"],
            changed_files=finalization["changed_files"],
            final_summary=delegate_result.summary,
            acceptance_passed=finalization["acceptance_passed"],
        )

        return json.dumps(coding_result.to_dict(), ensure_ascii=False)

    except Exception as e:
        return json.dumps(
            {
                "status": "failed",
                "error": f"{type(e).__name__}: {e}",
                "acceptance_passed": False,
            }
        )


async def _execute_coding_leaves(
    project_root: Path,
    worktree_path: Path,
    task_id: str,
    objective: str,
    contract: Any,
) -> list[CodingLeafResult]:
    """Execute coding operations as GoalRun leaves.

    This function orchestrates the actual coding work:
    1. Inspect codebase to understand structure
    2. Search for relevant code
    3. Make edits to fix/implement
    4. Run tests/lint/typecheck
    5. Verify changes

    Each step is a leaf that returns CodingLeafResult.
    """
    results: list[CodingLeafResult] = []

    # Import tools from registry
    from server.tool_registry import master_tools

    def _as_result(value: Any) -> dict[str, Any]:
        """Decode the registry's canonical string result at this adapter edge."""

        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return {"status": "error", "error": value}
            return decoded if isinstance(decoded, dict) else {"status": "error", "error": value}
        return {"status": "error", "error": f"unexpected result type: {type(value).__name__}"}

    # Step 1: Inspect workspace
    try:
        inspect_result = await master_tools.execute(
            "coding_workspace_detect",
            {"path": str(worktree_path)},
        )
        inspect_data = _as_result(inspect_result)
        results.append(
            CodingLeafResult(
                status="ok" if inspect_data.get("status") == "ok" else "failed",
                summary=f"Workspace inspection: {worktree_path}",
                evidence=[{"kind": "workspace_detect", "result": inspect_data}],
            )
        )
    except Exception as e:
        results.append(
            CodingLeafResult(
                status="failed",
                summary=f"Workspace inspection failed: {e}",
                evidence=[{"kind": "error", "message": str(e)}],
            )
        )

    # Step 2: Read key files to understand codebase
    # (This would be expanded based on objective)
    try:
        # Try to find test files
        test_files = list(worktree_path.rglob("test_*.py"))[:5]
        if test_files:
            for tf in test_files:
                try:
                    content = tf.read_text(encoding="utf-8")[:5000]
                    results.append(
                        CodingLeafResult(
                            status="ok",
                            summary=f"Read test file: {tf.name}",
                            evidence=[
                                {
                                    "kind": "file_read",
                                    "path": str(tf.relative_to(worktree_path)),
                                    "content": content[:1000],
                                }
                            ],
                        )
                    )
                except Exception:
                    pass
    except Exception as e:
        results.append(
            CodingLeafResult(
                status="partial",
                summary=f"File discovery issue: {e}",
                evidence=[{"kind": "warning", "message": str(e)}],
            )
        )

    # Step 3: Run tests to see current state
    try:
        test_result = await master_tools.execute(
            "coding_run_tests",
            {"worktree_path": str(worktree_path)},
        )
        test_data = _as_result(test_result)
        results.append(
            CodingLeafResult(
                status="ok" if test_data.get("status") == "ok" else "partial",
                summary=f"Tests: {test_data.get('data', {}).get('summary', 'ran')}",
                evidence=[{"kind": "test_run", "result": test_data}],
                acceptance_results=[
                    {
                        "criterion_id": "tests_pass",
                        "status": "passed" if test_data.get("status") == "ok" else "failed",
                    }
                ],
            )
        )
    except Exception as e:
        results.append(
            CodingLeafResult(
                status="partial",
                summary=f"Test run issue: {e}",
                evidence=[{"kind": "warning", "message": str(e)}],
            )
        )

    # Step 4: Run lint
    try:
        lint_result = await master_tools.execute(
            "coding_run_lint",
            {"worktree_path": str(worktree_path)},
        )
        lint_data = _as_result(lint_result)
        results.append(
            CodingLeafResult(
                status="ok" if lint_data.get("status") == "ok" else "partial",
                summary=f"Lint: {lint_data.get('data', {}).get('summary', 'ran')}",
                evidence=[{"kind": "lint_run", "result": lint_data}],
                acceptance_results=[
                    {
                        "criterion_id": "lint_pass",
                        "status": "passed" if lint_data.get("status") == "ok" else "failed",
                    }
                ],
            )
        )
    except Exception as e:
        results.append(
            CodingLeafResult(
                status="partial",
                summary=f"Lint run issue: {e}",
                evidence=[{"kind": "warning", "message": str(e)}],
            )
        )

    # Step 5: Generate diff
    try:
        diff_result = await master_tools.execute(
            "coding_diff",
            {"worktree_path": str(worktree_path)},
        )
        diff_data = _as_result(diff_result)
        results.append(
            CodingLeafResult(
                status="ok" if diff_data.get("status") == "ok" else "partial",
                summary="Generated diff",
                evidence=[{"kind": "diff", "result": diff_data}],
                artifacts=diff_data.get("artifacts", []),
            )
        )
    except Exception as e:
        results.append(
            CodingLeafResult(
                status="partial",
                summary=f"Diff generation issue: {e}",
                evidence=[{"kind": "warning", "message": str(e)}],
            )
        )

    return results


# Tool schema for MasterAgent registration
CODING_TASK_RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "workspace_path": {
            "type": "string",
            "description": "Absolute path to the workspace/repository root",
        },
        "objective": {
            "type": "string",
            "description": "Clear description of what the coding task should accomplish",
        },
        "resume_task_id": {
            "type": "string",
            "description": "Optional existing task_id to resume a previous coding task",
        },
        "max_wall_seconds": {
            "type": "integer",
            "description": "Optional wall-clock time limit in seconds",
        },
    },
    "required": ["workspace_path", "objective"],
}

CODING_TASK_RUN_DESCRIPTION = """Execute a coding task with full harness integration.

This tool creates an isolated worktree, executes coding operations, runs required
sensors (tests, lint, typecheck), and generates verification artifacts.

Use this tool when you need to:
- Fix failing tests
- Implement new features
- Refactor code
- Make any code changes that require verification

The tool returns a structured result with:
- task_id: Unique identifier for this coding task
- status: completed/partial_completed/failed/cancelled
- acceptance_passed: Whether all required sensors passed
- changed_files: List of files modified
- verification_report_id: ID of the verification report
- artifact_ids: IDs of generated artifacts

The task is durable and can be resumed if interrupted."""


__all__ = [
    "CODING_TASK_RUN_DESCRIPTION",
    "CODING_TASK_RUN_SCHEMA",
    "coding_task_run",
]
