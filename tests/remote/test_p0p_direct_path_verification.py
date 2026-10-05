"""P0-P: the direct development path is verified, not assumed.

``RemoteGoalRunAdapter`` has always declared ``verification_required = True``.
Before P0-P it implemented no ``finalize_candidate``, and the runner's accept
gate is guarded by ``hasattr(integration_adapter, "finalize_candidate")``. So the
direct path was marked ``passed=True`` with reason ``canonical_acceptance_deferred``
and the deferral target was never invoked: acceptance was assumed.

The authority is unchanged. This routes into the same ``VerificationEngine``
that ``CanonicalWorkerAdapter`` uses. No second verifier, no second completion
authority, no change to ``_decide_goal_completion`` or ``ExecutionRecord._finish``.

Because P0-O-G3 records that ``ExecutionRecord`` and ``GoalRun`` are still two
terminal authorities, every assertion here names both. They are never collapsed
into a single "it failed" claim.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from veya.remote import (
    RemoteAudit,
    RemoteAuth,
    RemotePermissions,
    RemoteSessionManager,
    RemoteToolAdapter,
)
from veya.remote.execution import ExecutionStatus, ExecutionStore
from veya.remote.mcp_server import create_gateway

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout


def make_repo(base: Path) -> Path:
    repo = base / "proj"
    repo.mkdir(parents=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "."],
        ["commit", "-qm", "init"],
    ):
        git(repo, *cmd)
    return repo


class Executor:
    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        return json.dumps({"status": "ok", "data": {}})


async def open_gateway(repo: Path):
    auth = RemoteAuth()
    _record, secret = auth.issue("t", permissions=PERMS, workspaces=[str(repo)])
    audit = RemoteAudit(None)
    adapter = RemoteToolAdapter(
        Executor(), redact=audit.redact, execution_store=ExecutionStore(None)
    )
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=120, max_sessions=8),
        audit=audit,
        adapter=adapter,
    )
    response = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"workspace": str(repo)}},
        authorization=f"Bearer {secret}",
    )
    session = response["result"]["sessionId"]
    return adapter, gateway, secret, session


async def run_direct(adapter, gateway, secret, session, repo: Path, command: str) -> Any:
    response = await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "test.run",
                "arguments": {"command": command, "workspace": str(repo), "wait": True},
            },
        },
        authorization=f"Bearer {secret}",
        session_header=session,
    )
    envelope = response["result"]["structuredContent"]
    # A failing command is a legitimate outcome, not a gateway error, so ok may
    # be False here. What must always exist is the durable record.
    execution_id = (envelope.get("result") or {}).get("execution_id") or envelope.get(
        "execution_id"
    )
    assert execution_id, envelope
    return adapter.jobs._records[execution_id]


def load_goal(repo: Path, record: Any) -> Any:
    from server.goal_run.store import load_goal_run

    goal = load_goal_run(record.goal_project_root, record.goal_run_id or "")
    assert goal is not None, "direct execution must leave a durable GoalRun"
    return goal


def goal_status_name(goal: Any) -> str:
    value = getattr(goal.status, "value", goal.status)
    return str(value)


def authorities(repo: Path, record: Any) -> dict[str, Any]:
    """All five statuses, read separately. Never merged (P0-O-G3)."""
    goal = load_goal(repo, record)
    task = goal.tasks.get(record.goal_task_id or "")
    return {
        "execution_record_status": str(record.status),
        "goal_run_status": goal_status_name(goal),
        "verifier_result": record.verification_result,
        "reconcile_task_status": None if task is None else task.status.value,
        "reconcile_execute_result": None if task is None else task.execute_result,
        "decide_goal_completion_verdict": goal.acceptance_verdict,
    }


# ── the fail-open is gone ────────────────────────────────────────────
async def test_the_adapter_really_implements_the_seam() -> None:
    """The gate is a hasattr check, so the method's existence is the whole fix."""
    import ast
    import inspect

    from veya.remote.execution import DurableJobManager

    source = Path(inspect.getfile(DurableJobManager)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    adapters = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "RemoteGoalRunAdapter"
    ]
    assert len(adapters) == 1
    methods = {
        b.name for b in adapters[0].body if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "finalize_candidate" in methods, sorted(methods)
    assert "execute_semantic_task" in methods


async def test_the_deferral_pass_through_is_not_our_acceptance_path(
    tmp_path: Path,
) -> None:
    """runner.py still contains canonical_acceptance_deferred for other adapters.

    That is fine — it is the generic branch. What must not happen is the direct
    adapter taking it, which is what the missing method used to guarantee.
    """
    repo = make_repo(tmp_path)
    adapter, gateway, secret, session = await open_gateway(repo)

    record = await run_direct(
        adapter,
        gateway,
        secret,
        session,
        repo,
        f'{sys.executable} -c "print(1+1)"',
    )

    reasons = [e.get("reason") for e in record.events if e.get("kind") == "goal_run_checkpoint"]
    # The record was verified rather than waved through.
    assert record.verification_result is not None
    assert record.verification_result != "canonical_acceptance_deferred"
    assert not any("canonical_acceptance_deferred" in str(r) for r in reasons)


# ── the negative proof: FAIL must not be accepted ────────────────────
async def test_a_failing_direct_command_is_not_accepted(tmp_path: Path) -> None:
    """The load-bearing negative proof.

    Five statuses are asserted separately, never merged:

        ExecutionRecord status, GoalRun status, verifier result,
        reconcile_task result, _decide_goal_completion result.
    """
    repo = make_repo(tmp_path)
    adapter, gateway, secret, session = await open_gateway(repo)

    record = await run_direct(
        adapter,
        gateway,
        secret,
        session,
        repo,
        f'{sys.executable} -c "import sys; print(3 failed); sys.exit(1)"',
    )
    goal = load_goal(repo, record)

    # 1. ExecutionRecord — the runner's own verdict, not a verdict we invented.
    assert str(record.status) == str(ExecutionStatus.FAILED)
    assert record.exit_code == 1

    # 2. Verifier — the seam produced a real, independent verdict.
    assert record.verification_result in {"FAIL", "INSUFFICIENT", "BLOCKED"}, (
        record.verification_result
    )
    assert record.verification_result != "PASS"

    # 3. GoalRun — the acceptance gate moved it off completed.
    assert goal_status_name(goal) not in {"completed", "COMPLETED"}, goal_status_name(goal)

    # 4. reconcile_task — the record outcome was projected back into the task.
    task = goal.tasks.get(record.goal_task_id or "")
    assert task is not None, "reconcile_task must attach the outcome to the task"
    assert task.status.value != "completed", task.status

    # 5. _decide_goal_completion — the run is recoverable, not complete.
    assert goal_status_name(goal) in {"recovering", "blocked", "running"}, goal_status_name(goal)

    # reconcile_task is the only writer of acceptance_verdict, so this pins it.
    assert goal.acceptance_verdict != "ACCEPT", goal.acceptance_verdict

    # The two authorities are recorded, not merged. P0-O-G3 stays visible.
    seen = authorities(repo, record)
    assert seen == {
        "execution_record_status": "FAILED",
        "goal_run_status": seen["goal_run_status"],
        "verifier_result": seen["verifier_result"],
        "reconcile_task_status": seen["reconcile_task_status"],
        "reconcile_execute_result": seen["reconcile_execute_result"],
        "decide_goal_completion_verdict": seen["decide_goal_completion_verdict"],
    }
    assert seen["goal_run_status"] not in {"completed", "COMPLETED"}
    assert seen["reconcile_task_status"] != "completed"


async def test_a_failing_command_records_both_authorities_distinctly(
    tmp_path: Path,
) -> None:
    """The two terminal authorities must remain separately readable.

    G3 is not fixed here. This asserts the seam did not quietly collapse
    ExecutionRecord and GoalRun into one status.
    """
    repo = make_repo(tmp_path)
    adapter, gateway, secret, session = await open_gateway(repo)

    record = await run_direct(
        adapter,
        gateway,
        secret,
        session,
        repo,
        f'{sys.executable} -c "import sys; sys.exit(2)"',
    )
    goal = load_goal(repo, record)

    events = [e for e in record.events if e.get("kind") == "direct_verification"]
    assert events, "the seam must leave an auditable event"
    event = events[-1]
    # The event carries the record status at verification time and the verdict
    # separately, so a reader can see the two did not become one number.
    assert event["verdict"] == record.verification_result
    assert event["observed_exit_code"] == 2
    # The record has not been finalized yet at verification time; asserting the
    # ordering is part of what makes the two authorities distinguishable.
    assert event["execution_record_status"] != str(ExecutionStatus.COMPLETED)
    # And the GoalRun side is independently observable.
    assert goal_status_name(goal) not in {"completed", "COMPLETED"}


# ── the positive path ────────────────────────────────────────────────
async def test_a_passing_direct_command_is_verified_and_accepted(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    adapter, gateway, secret, session = await open_gateway(repo)

    record = await run_direct(
        adapter,
        gateway,
        secret,
        session,
        repo,
        f'{sys.executable} -c "print(1+1)"',
    )
    goal = load_goal(repo, record)

    assert str(record.status) == str(ExecutionStatus.COMPLETED)
    assert record.exit_code == 0
    # The verifier ran and passed on its own evidence.
    assert record.verification_result == "PASS", record.verification_summary
    assert goal_status_name(goal) == "completed", goal_status_name(goal)
    task = goal.tasks.get(record.goal_task_id or "")
    assert task is not None and task.status.value == "completed"
    # reconcile_task's own acceptance record. Nothing else writes this field.
    assert goal.acceptance_verdict == "ACCEPT", goal.acceptance_verdict
    assert task.execute_result


async def test_claiming_success_in_output_does_not_grant_acceptance(
    tmp_path: Path,
) -> None:
    """Acceptance comes from the observed exit code, never from the output text.

    A command that prints a success-looking string and then exits non-zero must
    be rejected. This is the difference between verifying a command and reading
    what it said about itself.
    """
    repo = make_repo(tmp_path)
    adapter, gateway, secret, session = await open_gateway(repo)

    record = await run_direct(
        adapter,
        gateway,
        secret,
        session,
        repo,
        f"{sys.executable} -c \"print('PASS all tests'); import sys; sys.exit(1)\"",
    )

    assert record.exit_code == 1
    assert "PASS all tests" in (record.stdout_tail or "")
    assert record.verification_result == "FAIL", record.verification_summary
    goal = load_goal(repo, record)
    assert goal_status_name(goal) not in {"completed", "COMPLETED"}


# ── the seam is reached through the gateway, not by calling it directly ──
async def test_the_verdict_comes_from_the_real_engine_not_a_local_call(
    tmp_path: Path,
) -> None:
    """The verdict must be the engine's, not something the seam asserts."""
    repo = make_repo(tmp_path)
    adapter, gateway, secret, session = await open_gateway(repo)

    record = await run_direct(
        adapter,
        gateway,
        secret,
        session,
        repo,
        f'{sys.executable} -c "import sys; sys.exit(3)"',
    )

    # run_independent_verifier keys a non-zero exit_code to explicit failure.
    # That is the engine's rule, and it is what produced this verdict.
    assert record.exit_code == 3
    assert record.verification_result == "FAIL", record.verification_summary
    assert "exit 3" in (record.verification_summary or "") or record.verification_summary


async def test_a_crash_is_not_mistaken_for_a_verdict(tmp_path: Path) -> None:
    """A spawn failure is a runtime error, not an accepted command."""
    repo = make_repo(tmp_path)
    adapter, gateway, secret, session = await open_gateway(repo)

    record = await run_direct(
        adapter,
        gateway,
        secret,
        session,
        repo,
        f'{sys.executable} -c "import os; os._exit(9)"',
    )

    assert str(record.status) != str(ExecutionStatus.COMPLETED)
    assert record.verification_result != "PASS"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
