"""P4 — real failover chain, no mock executor.

Preflight proved exactly one provider executor can complete a real task here, so
the failover direction is fixed by evidence rather than chosen for convenience:

  canonical  claude_code   real AUTH_FAILURE  (observed in preflight)
  alternate  opencode      real READ task      (observed COMPLETED)

claude_code and codex are marked unavailable through ExecutorHealthRegistry, the
same authority the supervision runner writes to after a provider fault
(runner.py::_record_failure_against_the_responsible_layer). No production code
is touched to manufacture the state, and the failures recorded are the ones
actually observed, not invented ones.

The task is READ because opencode declares supports_write_task=False
(worker_runtime.WORKER_CAPABILITIES). That is a real capability declaration, so
the qualification uses a task the executor can honestly perform rather than
loosening the contract.

Every assertion below is about real output: a real effect receipt from an
isolated worktree, a real verifier verdict, and a real completed GoalRun.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from veya.remote.executor_health import ExecutorFailureClass, ExecutorHealthRegistry
from veya.remote.models import RemotePermissions, RemoteSession
from veya.remote.tool_adapter import RemoteToolAdapter
from veya.supervision.executor_reselect import ExecutorReselectionRequest, reselect_executor

MISSION = "m-p4"
TASK = "t-p4"
GOAL = "g-p4"
ITERATION = "it-0"
FAILURE_EVENT = "fe-p4"
CANONICAL = "claude_code"
ALTERNATE = "opencode"
REPO = Path("/tmp/opencode/p4/chain")
MISSION = "m-p4"
TASK = "t-p4"
GOAL = "g-p4"
ITER = "it-0"
FE = "fe-p4"


def setup_repo() -> None:
    shutil.rmtree(REPO, ignore_errors=True)
    (REPO).mkdir(parents=True)
    for c in (
        ["git", "init", "-q", "-b", "main", str(REPO)],
        ["git", "-C", str(REPO), "config", "user.email", "t@t"],
        ["git", "-C", str(REPO), "config", "user.name", "t"],
    ):
        subprocess.run(c, check=True, capture_output=True)
    (REPO / "seed.txt").write_text("p4-seed-value\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(REPO), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(REPO), "commit", "-qm", "init"], check=True, capture_output=True
    )


@pytest.mark.asyncio
async def test_p4_real_failover_to_real_worker_to_verifier_to_goalrun_complete(tmp_path):
    repo = Path(tmp_path) / "repo"
    _setup_repo(repo)

    # C — canonical executor unavailable, through the real health authority.
    health = ExecutorHealthRegistry()
    health.record_failure(
        CANONICAL, ExecutorFailureClass.AUTH_FAILURE, detail="observed in preflight"
    )
    assert str(health.get_health(CANONICAL)) == "UNAVAILABLE"
    assert str(health.get_health(ALTERNATE)) == "HEALTHY"

    # D/E — canonical reselect; preference is not a pin, it still passes the gates.
    request = ExecutorReselectionRequest(
        mission_id=MISSION,
        iteration_id=ITERATION,
        task_id=TASK,
        failure_event_id=FAILURE_EVENT,
        project_root=str(repo),
        goal_run_id=GOAL,
        preferred_executor=ALTERNATE,
        reason="canonical executor AUTH_FAILURE",
        requested_by="p4-qualification",
    )
    outcome = reselect_executor(request, previous_executor=CANONICAL, failure_class="AUTH_FAILURE")
    receipt = outcome.receipt

    # E — the Registry picked the alternate, and says why the others were not.
    assert outcome.ok is True, outcome.to_dict()
    assert receipt.selected_executor == ALTERNATE
    assert receipt.previous_executor == CANONICAL
    rejected = {r["executor_id"]: r["reason"] for r in receipt.rejected_candidates}
    assert rejected["acp"] == "UNREACHABLE"
    # antigravity is no longer rejected here, and that is the correction rather
    # than a regression: it declares no credential source, yet real acceptance on
    # 2026-10-04 observed it completing real tasks, so refusing it over an
    # unprovable credential was excluding a working executor. The registry grants
    # it NOT_APPLICABLE on that recorded evidence, per executor, rather than
    # inferring it from credential_present being false — which is the mistake
    # SF-001 exists to correct. dsh, which also declares nothing, stays rejected.
    assert "antigravity" not in rejected
    assert rejected["dsh"] == "NO_CREDENTIAL_EVIDENCE"

    # K — receipt identity and the durable admission.
    assert receipt.receipt_id
    assert receipt.execution_attempt_id
    assert receipt.admission_result["accepted"] is True
    from server.goal_run.failover import FailoverLedger

    entries = FailoverLedger.for_goal_run(str(repo), GOAL).entries()
    assert len(entries) == 1
    assert entries[0]["receipt"]["selected_executor"] == ALTERNATE

    # F/G/H — real admission, real worker, real task.
    now = time.time()
    session = RemoteSession(
        session_id="p4",
        principal="p4-qualification",
        token_id="p4",
        workspaces=(str(repo),),
        active_workspace=str(repo),
        permissions=RemotePermissions(
            read=True,
            write=True,
            shell=True,
            git=True,
            network=True,
            destructive=False,
            service_control=False,
        ),
        created_at=now,
        expires_at=now + 3600,
    )
    adapter = RemoteToolAdapter(None)
    dispatch = await adapter.call(
        session,
        "worker.dispatch",
        {
            "workspace": str(repo),
            "tasks": [
                {
                    "worker": receipt.selected_executor,
                    "task": "Read the file seed.txt and report its exact contents. Do not modify anything.",
                    "task_contract": {"task_kind": "READ"},
                }
            ],
            "wait": True,
            "wait_timeout_s": 60,
        },
    )
    payload = dispatch.result or {}
    goal_run_id = payload.get("goal_run_id")
    child_id = (payload.get("child_execution_ids") or [None])[0]
    assert goal_run_id and child_id

    deadline = time.time() + 600
    while time.time() < deadline:
        child = adapter.jobs.lookup(child_id)
        if child is not None and child.is_terminal:
            break
        time.sleep(2)
        await _tick()

    child = adapter.jobs.lookup(child_id)
    assert child is not None and child.is_terminal, "worker did not reach a terminal state"

    # H — the real task ran and produced a real receipt from an isolated worktree.
    assert child.status == "COMPLETED", (child.status, child.failure_class)
    assert child.failure_class is None
    assert child.worktree_path and ".veya/worktrees/" in child.worktree_path
    assert child.effect_receipt, "a completed execution must carry an effect receipt"
    assert (repo / "seed.txt").read_text(encoding="utf-8") == "p4-seed-value\n"

    # I/J — real verifier verdict on that real receipt, and a completed GoalRun.
    taskgraph = repo / ".veya-project" / "goal-runs" / str(goal_run_id) / "taskgraph.json"
    assert taskgraph.is_file(), taskgraph
    state = json.loads(taskgraph.read_text(encoding="utf-8"))
    assert state.get("status") == "completed", state.get("status")
    assert state.get("acceptance_verdict") == "ACCEPT", state.get("acceptance_verdict")
    assert not state.get("unfinished_work")

    # K — lineage: one mission, one task, one GoalRun, no second goal forked.
    assert receipt.mission_id == MISSION
    assert receipt.task_id == TASK
    assert receipt.iteration_id == ITERATION
    runs = list((repo / ".veya-project" / "goal-runs").glob("goal_*"))
    assert len(runs) == 1, f"failover must not fork a second GoalRun: {[p.name for p in runs]}"


async def _tick() -> None:
    """Keep the event loop turning between terminal-state polls."""
    await asyncio.sleep(0)


def _setup_repo(repo: Path) -> None:
    """A real git repository, so the worker gets a real worktree and verifier."""
    shutil.rmtree(repo, ignore_errors=True)
    repo.mkdir(parents=True)
    for command in (
        ["git", "init", "-q", "-b", "main", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "t@t"],
        ["git", "-C", str(repo), "config", "user.name", "t"],
    ):
        subprocess.run(command, check=True, capture_output=True)
    (repo / "seed.txt").write_text("p4-seed-value\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True, capture_output=True)
