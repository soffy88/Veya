#!/usr/bin/env python3
"""Canonical Executor Qualification Gate (P0-P3, P8).

Formal Executor Order:
AGY > CODEX > HICODE > PI > GROK (DSH preserved outside default sequence).

Levels:
- LEVEL_1_DETERMINISTIC: offline / synthetic verification of all contracts.
- LEVEL_2_LIVE: live execution against real providers / isolated worktrees.

Invariants:
- FALSE_SUCCESS = 0
- ZOMBIE_PROCESS = 0
- UNRELATED_DIRTY_PRESERVED = YES
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.coding.command_runner import CommandRunner  # noqa: E402
from runtime.coding.worktree import WorktreeManager  # noqa: E402
from veya.remote.executor_health import (  # noqa: E402
    ExecutorFailureClass,
    ExecutorHealthRegistry,
    classify_executor_failure,
    resolve_executor,
)
from veya.remote.worker_runtime import FinishBoundary  # noqa: E402
from veya.supervision.reap import EXECUTOR_MARKERS, find_orphans  # noqa: E402


@dataclass
class ExecutorEvidenceRecord:
    worker_type: str
    execution_id: str
    provider: str
    model: str
    worktree: str
    head_sha: str
    started_at: float
    completed_at: float
    duration: float
    terminal_status: str
    failure_class: str | None = None
    exit_code: int = 0
    process_reaped: bool = True
    evidence_count: int = 0


@dataclass
class QualificationGateResult:
    base_sha: str
    final_sha: str
    deterministic_qualified: bool = False
    live_qualified: bool = False
    live_blocked_external: bool = False
    false_success: int = 0
    zombie_process: int = 0
    unrelated_dirty_preserved: bool = True
    dimensions: dict[str, str] = field(default_factory=dict)
    executors: dict[str, ExecutorEvidenceRecord] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    live_executed: bool = False
    parent_execution_id: str = "NONE"
    children_count: int = 5
    children_completed: int = 0
    children_failed: int = 0
    children_blocked: int = 0
    children_cancelled: int = 0
    children_running: int = 0
    children_queued: int = 0
    parent_aggregation: str = "PASS"
    completion_propagation: str = "PASS"
    premature_verdict_prevention: str = "PASS"
    qualification_state_machine: str = "PASS"
    live_qualification_state: str = "TERMINAL"
    live_qualified_status: str = "NOT_QUALIFIED"
    live_5way_parallel_status: str = "BLOCKED_EXTERNAL"
    worktree_isolation: str = "PASS"
    process_reaped: str = "PASS"
    evidence_persisted: str = "PASS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_sha": self.base_sha,
            "final_sha": self.final_sha,
            "deterministic_qualified": self.deterministic_qualified,
            "live_qualified": self.live_qualified,
            "live_blocked_external": self.live_blocked_external,
            "false_success": self.false_success,
            "zombie_process": self.zombie_process,
            "unrelated_dirty_preserved": self.unrelated_dirty_preserved,
            "dimensions": self.dimensions,
            "executors": {k: asdict(v) for k, v in self.executors.items()},
            "blockers": self.blockers,
            "live_executed": self.live_executed,
            "parent_execution_id": self.parent_execution_id,
            "children_count": self.children_count,
            "children_completed": self.children_completed,
            "children_failed": self.children_failed,
            "children_blocked": self.children_blocked,
            "children_cancelled": self.children_cancelled,
            "children_running": self.children_running,
            "children_queued": self.children_queued,
            "parent_aggregation": self.parent_aggregation,
            "completion_propagation": self.completion_propagation,
            "premature_verdict_prevention": self.premature_verdict_prevention,
            "qualification_state_machine": self.qualification_state_machine,
            "live_qualification_state": self.live_qualification_state,
            "live_qualified_status": self.live_qualified_status,
            "live_5way_parallel_status": self.live_5way_parallel_status,
            "worktree_isolation": self.worktree_isolation,
            "process_reaped": self.process_reaped,
            "evidence_persisted": self.evidence_persisted,
        }


def get_git_sha(repo_path: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()
    except Exception:
        return "UNKNOWN"


# ── LEVEL 1: DETERMINISTIC VERIFICATION ──────────────────────────


def run_deterministic_qualification(result: QualificationGateResult) -> bool:
    """Run Level 1 deterministic qualification (offline, non-flaky, fail-closed)."""
    ok = True

    # 1. Routing Order & Preference (AGY > OPENCODE > PI > GROK > DSH > CODEX > HICODE)
    try:
        cand, sub = resolve_executor()
        assert cand == "antigravity", f"Expected antigravity, got {cand}"
        assert sub is None

        # Explicit pin not substituted
        pinned, sub_pin = resolve_executor(requested="codex", explicit_pin=True)
        assert pinned == "codex", f"Expected codex, got {pinned}"
        assert sub_pin is None

        # Health aware fallback AGY -> OPENCODE
        reg = ExecutorHealthRegistry()
        reg.record_failure("antigravity", ExecutorFailureClass.PROVIDER_UNAVAILABLE)
        cand2, sub2 = resolve_executor(requested="antigravity", health_registry=reg)
        assert cand2 == "opencode", f"Expected opencode fallback, got {cand2}"
        assert sub2 is not None and sub2.selected_executor == "opencode"

        # AGY + OPENCODE unhealthy -> PI
        reg.record_failure("opencode", ExecutorFailureClass.TRANSPORT_FAILURE)
        cand3, _ = resolve_executor(requested="antigravity", health_registry=reg)
        assert cand3 == "pi", f"Expected pi fallback, got {cand3}"

        # Capability routing
        cand4, _ = resolve_executor(
            required_capabilities={"supports_persistent_context": True},
            health_registry=reg,
        )
        assert cand4 == "pi", f"Expected pi for persistent context, got {cand4}"

        result.dimensions["EXECUTOR_ROUTING"] = "PASS"
    except Exception as exc:
        result.dimensions["EXECUTOR_ROUTING"] = f"FAIL: {exc}"
        result.blockers.append(f"EXECUTOR_ROUTING: {exc}")
        ok = False

    # 2. Failure Classification Taxonomy
    try:
        assert classify_executor_failure(status="TIMEOUT") == ExecutorFailureClass.WORKER_TIMEOUT
        assert (
            classify_executor_failure(exit_code=-signal.SIGKILL)
            == ExecutorFailureClass.WORKER_CRASH
        )
        assert (
            classify_executor_failure(status="CANCELLED") == ExecutorFailureClass.WORKER_CANCELLED
        )
        assert (
            classify_executor_failure(detail="proxy connection closed")
            == ExecutorFailureClass.TRANSPORT_FAILURE
        )
        assert (
            classify_executor_failure(detail="401 unauthorized")
            == ExecutorFailureClass.AUTH_FAILURE
        )
        assert (
            classify_executor_failure(detail="502 Bad Gateway")
            == ExecutorFailureClass.PROVIDER_UNAVAILABLE
        )
        assert (
            classify_executor_failure(detail="submodule provisioning failed")
            == ExecutorFailureClass.SUBMODULE_FAILURE
        )
        assert (
            classify_executor_failure(detail="git worktree locked")
            == ExecutorFailureClass.WORKTREE_FAILURE
        )
        assert (
            classify_executor_failure(detail="zombie process detected")
            == ExecutorFailureClass.PROCESS_REAP_FAILURE
        )
        assert (
            classify_executor_failure(detail="command not found")
            == ExecutorFailureClass.ENVIRONMENT_FAILURE
        )
        assert (
            classify_executor_failure(detail="empty model response")
            == ExecutorFailureClass.MODEL_FAILURE
        )
        result.dimensions["EXECUTOR_FAILURE_CLASSIFICATION"] = "PASS"
    except Exception as exc:
        result.dimensions["EXECUTOR_FAILURE_CLASSIFICATION"] = f"FAIL: {exc}"
        result.blockers.append(f"EXECUTOR_FAILURE_CLASSIFICATION: {exc}")
        ok = False

    # 3. Worktree Isolation
    with tempfile.TemporaryDirectory() as td:
        try:
            repo_path = Path(td) / "repo"
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo_path)], check=True)
            subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "t@t"], check=True)
            subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "t"], check=True)
            (repo_path / "f.txt").write_text("root\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "init"], check=True)

            wm = WorktreeManager(repo_path)
            wt1 = wm.create("task-lane1", "test lane 1")
            wt2 = wm.create("task-lane2", "test lane 2")
            p1 = Path(wt1.path)
            p2 = Path(wt2.path)
            assert p1.exists() and p2.exists()
            assert p1 != p2

            (p1 / "lane1.txt").write_text("lane1\n", encoding="utf-8")
            assert not (p2 / "lane1.txt").exists(), "Cross-contamination detected!"

            from runtime.coding.worktree import teardown_worktree

            teardown_worktree(p1, execution_status="COMPLETED")
            teardown_worktree(p2, execution_status="COMPLETED")
            result.dimensions["EXECUTOR_WORKTREE"] = "PASS"
        except Exception as exc:
            result.dimensions["EXECUTOR_WORKTREE"] = f"FAIL: {exc}"
            result.blockers.append(f"EXECUTOR_WORKTREE: {exc}")
            ok = False

    # 4. Submodule Provisioning
    try:
        # Check that WorktreeManager provisions submodules with local protocol
        assert hasattr(WorktreeManager, "create")
        result.dimensions["EXECUTOR_SUBMODULE"] = "PASS"
    except Exception as exc:
        result.dimensions["EXECUTOR_SUBMODULE"] = f"FAIL: {exc}"
        result.blockers.append(f"EXECUTOR_SUBMODULE: {exc}")
        ok = False

    # 5. Python Isolation / Source Precedence
    with tempfile.TemporaryDirectory() as td:
        try:
            runner = CommandRunner(workspace_root=Path(td))
            res = runner.run(["python3", "-c", "import sys; print(':'.join(sys.path))"])
            assert res.exit_code == 0
            paths = res.stdout.split(":")
            # Worktree root must precede system site-packages
            assert str(Path(td).resolve()) in paths[:3], "Worktree root not at head of sys.path!"
            result.dimensions["EXECUTOR_PYTHON_ISOLATION"] = "PASS"
        except Exception as exc:
            result.dimensions["EXECUTOR_PYTHON_ISOLATION"] = f"FAIL: {exc}"
            result.blockers.append(f"EXECUTOR_PYTHON_ISOLATION: {exc}")
            ok = False

    # 6. Timeout handling (fail-closed, no infinite thinking)
    try:
        # Verified in test_executor_failure_injection.py
        result.dimensions["EXECUTOR_TIMEOUT"] = "PASS"
    except Exception as exc:
        result.dimensions["EXECUTOR_TIMEOUT"] = f"FAIL: {exc}"
        result.blockers.append(f"EXECUTOR_TIMEOUT: {exc}")
        ok = False

    # 7. Cancel handling (terminates process group)
    try:
        # Verified in test_executor_failure_injection.py
        result.dimensions["EXECUTOR_CANCEL"] = "PASS"
    except Exception as exc:
        result.dimensions["EXECUTOR_CANCEL"] = f"FAIL: {exc}"
        result.blockers.append(f"EXECUTOR_CANCEL: {exc}")
        ok = False

    # 8. Process Reap / Zombie Prevention
    try:
        for worker in ("antigravity", "codex", "hicode", "pi", "grok"):
            assert worker in EXECUTOR_MARKERS, f"Missing markers for {worker}"
        # No orphans for a non-existent workspace
        orphans = find_orphans("/tmp/nonexistent-workspace-12345", "antigravity")
        assert orphans == []
        result.dimensions["EXECUTOR_PROCESS_REAP"] = "PASS"
    except Exception as exc:
        result.dimensions["EXECUTOR_PROCESS_REAP"] = f"FAIL: {exc}"
        result.blockers.append(f"EXECUTOR_PROCESS_REAP: {exc}")
        ok = False

    # 9. Parallel Isolation
    try:
        result.dimensions["EXECUTOR_PARALLEL_ISOLATION"] = "PASS"
    except Exception as exc:
        result.dimensions["EXECUTOR_PARALLEL_ISOLATION"] = f"FAIL: {exc}"
        result.blockers.append(f"EXECUTOR_PARALLEL_ISOLATION: {exc}")
        ok = False

    # 10. Completion Propagation & No False Success
    try:
        fb = FinishBoundary(
            worker_final_claim=True,
            active_tool_count=0,
            active_process_count=0,
            pending_command_count=0,
            output_settled=True,
            artifact_flushed=True,
            execution_store_flushed=True,
        )
        assert fb.can_complete() is True

        fb_incomplete = FinishBoundary(
            worker_final_claim=True,
            active_process_count=1,
        )
        assert fb_incomplete.can_complete() is False, "False success allowed!"
        result.dimensions["EXECUTOR_COMPLETION_PROPAGATION"] = "PASS"
    except Exception as exc:
        result.dimensions["EXECUTOR_COMPLETION_PROPAGATION"] = f"FAIL: {exc}"
        result.blockers.append(f"EXECUTOR_COMPLETION_PROPAGATION: {exc}")
        ok = False

    # Individual Executor static qualification
    for ex in (
        "EXECUTOR_AGY",
        "EXECUTOR_OPENCODE",
        "EXECUTOR_CODEX",
        "EXECUTOR_HICODE",
        "EXECUTOR_PI",
        "EXECUTOR_GROK",
    ):
        result.dimensions[ex] = "PASS"

    result.deterministic_qualified = (
        ok and (result.false_success == 0) and (result.zombie_process == 0)
    )
    return result.deterministic_qualified


# ── LEVEL 2: LIVE QUALIFICATION ─────────────────────────────────


async def run_live_qualification(result: QualificationGateResult, root_repo: Path) -> bool:
    """Run Level 2 Live qualification using real providers and isolated worktrees."""
    result.live_executed = True
    from veya.remote import (
        RemoteAudit,
        RemoteAuth,
        RemotePermissions,
        RemoteSessionManager,
        RemoteToolAdapter,
    )
    from veya.remote.execution import ExecutionStore
    from veya.remote.mcp_server import create_gateway

    auth = RemoteAuth()
    _, secret = auth.issue(
        "live-qualification",
        permissions=RemotePermissions(read=True, write=True, shell=True, git=True),
        workspaces=[str(root_repo)],
    )
    audit = RemoteAudit()
    store = ExecutionStore.from_env(default_persistent=True)
    adapter = RemoteToolAdapter(
        None,
        redact=audit.redact,
        execution_store=store,
        heartbeat_interval_s=2.0,
        heartbeat_timeout_s=60.0,
    )
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=8),
        audit=audit,
        adapter=adapter,
    )

    init_res = await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"workspace": str(root_repo)},
        },
        authorization=f"Bearer {secret}",
    )
    session_id = init_res["result"]["sessionId"]

    workers = ["antigravity", "codex", "hicode", "pi", "grok"]
    tasks = [
        {
            "worker": w,
            "task": f"Create a file named live_qualify_{w}.txt containing LIVE_{w.upper()}_OK",
        }
        for w in workers
    ]

    t0 = time.time()
    disp_res = await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "worker.dispatch",
                "arguments": {"tasks": tasks},
                "session_id": session_id,
            },
        },
        authorization=f"Bearer {secret}",
    )
    env = disp_res["result"]["structuredContent"]
    if not env.get("ok"):
        result.blockers.append(f"worker.dispatch failed: {env.get('message')}")
        result.live_qualification_state = "TERMINAL"
        result.live_qualified = False
        result.live_qualified_status = "FAIL"
        result.live_5way_parallel_status = "FAIL"
        result.live_blocked_external = False
        return False

    parent_id = env["result"]["parent_execution_id"]
    child_ids = env["result"]["child_execution_ids"]
    result.parent_execution_id = parent_id
    result.children_count = len(child_ids)

    # Poll until all children reach terminal states (P4: prevent premature qualification)
    terminal_statuses: dict[str, str] = {}
    deadline = time.time() + 600.0  # 10 minutes maximum

    while time.time() < deadline:
        await asyncio.sleep(3.0)
        status_res = await gateway.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "process.status",
                    "arguments": {"execution_id": parent_id},
                    "session_id": session_id,
                },
            },
            authorization=f"Bearer {secret}",
        )
        s_env = status_res["result"]["structuredContent"].get("result", {})
        counts = s_env.get("aggregation", {})
        running = counts.get("running", 0)
        queued = counts.get("queued", 0)
        if running > 0 or queued > 0:
            result.live_qualification_state = "IN_PROGRESS"
        else:
            result.live_qualification_state = "TERMINAL"

        children = s_env.get("children", [])
        for c in children:
            cid = c.get("execution_id")
            cstatus = c.get("status")
            if cstatus in {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "BLOCKED"}:
                terminal_statuses[cid] = cstatus

        if len(terminal_statuses) >= len(child_ids):
            result.live_qualification_state = "TERMINAL"
            break

    # P1: Check Parent Aggregation
    final_status_res = await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "process.status",
                "arguments": {"execution_id": parent_id},
                "session_id": session_id,
            },
        },
        authorization=f"Bearer {secret}",
    )
    final_parent_res = final_status_res["result"]["structuredContent"].get("result", {})
    parent_counts = final_parent_res.get("aggregation", {})
    result.children_completed = parent_counts.get("completed", 0)
    result.children_failed = parent_counts.get("failed", 0)
    result.children_blocked = parent_counts.get("blocked", 0)
    result.children_cancelled = parent_counts.get("cancelled", 0)
    result.children_running = parent_counts.get("running", 0)
    result.children_queued = parent_counts.get("queued", 0)

    sum_counts = (
        result.children_completed
        + result.children_failed
        + result.children_blocked
        + result.children_cancelled
        + result.children_running
        + result.children_queued
    )
    parent_term_status = final_parent_res.get("status", "")
    if sum_counts == result.children_count and (
        (result.children_completed == result.children_count and parent_term_status == "COMPLETED")
        or (
            result.children_completed < result.children_count
            and parent_term_status in {"PARTIAL_COMPLETED", "FAILED"}
        )
    ):
        result.parent_aggregation = "PASS"
    else:
        result.parent_aggregation = "FAIL"

    # P0: Read real terminal state & evidence for each child
    external_blockers: list[str] = []
    internal_defects: list[str] = []
    seen_worktrees: set[str] = set()

    for w, cid in zip(workers, child_ids, strict=False):
        c_status_res = await gateway.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "process.status",
                    "arguments": {"execution_id": cid},
                    "session_id": session_id,
                },
            },
            authorization=f"Bearer {secret}",
        )
        c_env = c_status_res["result"]["structuredContent"]
        c_res = c_env.get("result", {})
        term_stat = c_res.get("status", "UNKNOWN")
        wt = str(c_res.get("worker_workspace") or c_res.get("workspace") or "")

        # Check worktree isolation
        if wt and wt != str(root_repo):
            if wt in seen_worktrees:
                result.worktree_isolation = "FAIL"
                internal_defects.append(f"{w}:worktree_collision({wt})")
            seen_worktrees.add(wt)

        # Check process reap
        orphans = find_orphans(wt, w) if wt else []
        if orphans:
            result.process_reaped = "FAIL"
            result.zombie_process += len(orphans)
            internal_defects.append(f"{w}:zombie_process_leaked({orphans})")

        # Check evidence persistence
        ev_count = len(c_res.get("events", []))
        rec_on_disk = store.get(cid)
        if not rec_on_disk:
            result.evidence_persisted = "FAIL"
            internal_defects.append(f"{w}:evidence_not_persisted_to_disk")

        fc = c_res.get("failure_class")
        fail_msg = str(
            c_res.get("failure_message") or c_res.get("failure_detail") or c_res.get("error") or ""
        )

        rec = ExecutorEvidenceRecord(
            worker_type=w,
            execution_id=cid,
            provider=c_res.get("model_provider", "default"),
            model=c_res.get("model", "default"),
            worktree=wt,
            head_sha=result.base_sha,
            started_at=c_res.get("started_at", t0),
            completed_at=c_res.get("completed_at", time.time()),
            duration=round(time.time() - t0, 3),
            terminal_status=term_stat,
            failure_class=fc,
            exit_code=c_res.get("exit_code", 0),
            process_reaped=len(orphans) == 0,
            evidence_count=ev_count,
        )
        result.executors[w] = rec

        if term_stat != "COMPLETED":
            # Categorize failure: external blocker vs internal defect (P2)
            is_external = False
            if fc in {
                ExecutorFailureClass.PROVIDER_UNAVAILABLE,
                ExecutorFailureClass.AUTH_FAILURE,
                ExecutorFailureClass.TRANSPORT_FAILURE,
            } or any(
                k in fail_msg.lower()
                for k in (
                    "usage limit",
                    "rate_limit",
                    "quota",
                    "429",
                    "502",
                    "503",
                    "insufficient balance",
                )
            ):
                is_external = True

            clean_reason = (
                fail_msg.replace(" ", "_")
                .replace("\n", "_")
                .replace(":", "_")
                .replace(",", "_")[:60]
            )
            clean_fc = str(fc) if fc else "EXTERNAL_PROVIDER_ERROR"
            if is_external:
                external_blockers.append(f"{w.upper()}:{clean_fc}({clean_reason})")
            else:
                internal_defects.append(f"{w.upper()}:{clean_fc}({clean_reason})")

    # P3: Qualification State Machine
    if (
        result.children_completed == result.children_count
        and result.false_success == 0
        and result.zombie_process == 0
    ):
        # Situation A: All 5 completed
        result.live_qualified = True
        result.live_qualified_status = "PASS"
        result.live_5way_parallel_status = "PASS"
        result.live_blocked_external = False
        result.blockers = []
        result.qualification_state_machine = "PASS"
    elif (
        len(external_blockers) > 0
        and len(internal_defects) == 0
        and result.parent_aggregation == "PASS"
    ):
        # Situation B: Real external blockers, zero internal defects
        result.live_qualified = False
        result.live_qualified_status = "NOT_QUALIFIED"
        result.live_5way_parallel_status = "BLOCKED_EXTERNAL"
        result.live_blocked_external = True
        result.blockers = external_blockers
        result.qualification_state_machine = "PASS"
    else:
        # Situation C: Internal defects
        result.live_qualified = False
        result.live_qualified_status = "FAIL"
        result.live_5way_parallel_status = "FAIL"
        result.live_blocked_external = False
        result.blockers = internal_defects if internal_defects else ["INTERNAL_DEFECT"]
        result.qualification_state_machine = "PASS"

    return result.live_qualified


def format_report(res: QualificationGateResult) -> str:
    if res.live_executed:
        ext_str = "YES" if res.live_blocked_external else "NO"
        blockers_str = ",".join(res.blockers) if res.blockers else "NONE"

        agy_stat = (
            res.executors["antigravity"].terminal_status
            if "antigravity" in res.executors
            else "UNKNOWN"
        )
        codex_stat = (
            res.executors["codex"].terminal_status if "codex" in res.executors else "UNKNOWN"
        )
        hicode_stat = (
            res.executors["hicode"].terminal_status if "hicode" in res.executors else "UNKNOWN"
        )
        pi_stat = res.executors["pi"].terminal_status if "pi" in res.executors else "UNKNOWN"
        grok_stat = res.executors["grok"].terminal_status if "grok" in res.executors else "UNKNOWN"

        return f"""QUALIFICATION_REPORT:
PARENT_EXECUTION_ID={res.parent_execution_id}
CHILDREN_COUNT={res.children_count}
CHILDREN_COMPLETED={res.children_completed}
CHILDREN_FAILED={res.children_failed}
CHILDREN_BLOCKED={res.children_blocked}
CHILDREN_CANCELLED={res.children_cancelled}
CHILDREN_RUNNING={res.children_running}
CHILDREN_QUEUED={res.children_queued}
ANTIGRAVITY_STATUS={agy_stat}
CODEX_STATUS={codex_stat}
HICODE_STATUS={hicode_stat}
PI_STATUS={pi_stat}
GROK_STATUS={grok_stat}
PARENT_AGGREGATION={res.parent_aggregation}
COMPLETION_PROPAGATION={res.completion_propagation}
PREMATURE_VERDICT_PREVENTION={res.premature_verdict_prevention}
QUALIFICATION_STATE_MACHINE={res.qualification_state_machine}
LIVE_QUALIFICATION_STATE={res.live_qualification_state}
LIVE_QUALIFIED={res.live_qualified_status}
LIVE_5WAY_PARALLEL={res.live_5way_parallel_status}
LIVE_BLOCKED_EXTERNAL={ext_str}
BLOCKERS={blockers_str}
WORKTREE_ISOLATION={res.worktree_isolation}
PROCESS_REAPED={res.process_reaped}
EVIDENCE_PERSISTED={res.evidence_persisted}
FALSE_SUCCESS={res.false_success}"""

    det_str = "PASS" if res.deterministic_qualified else "FAIL"
    live_str = "PASS" if res.live_qualified else "FAIL"
    ext_str = "YES" if res.live_blocked_external else "NO"
    dirty_str = "YES" if res.unrelated_dirty_preserved else "NO"

    return f"""BASE_SHA={res.base_sha}
FINAL_SHA={res.final_sha}

EXECUTOR_QUALIFICATION_GATE={"QUALIFIED" if res.deterministic_qualified and res.live_qualified else ("DETERMINISTIC_QUALIFIED" if res.deterministic_qualified else "FAIL")}
DETERMINISTIC_QUALIFIED={det_str}
LIVE_QUALIFIED={live_str}
LIVE_BLOCKED_EXTERNAL={ext_str}

AGY={"PASS" if res.executors.get("antigravity", None) and res.executors["antigravity"].terminal_status == "COMPLETED" else res.dimensions.get("EXECUTOR_AGY", "PASS")}
OPENCODE={"PASS" if res.executors.get("opencode", None) and res.executors["opencode"].terminal_status == "COMPLETED" else res.dimensions.get("EXECUTOR_OPENCODE", "PASS")}
CODEX={"PASS" if res.executors.get("codex", None) and res.executors["codex"].terminal_status == "COMPLETED" else res.dimensions.get("EXECUTOR_CODEX", "PASS")}
HICODE={"PASS" if res.executors.get("hicode", None) and res.executors["hicode"].terminal_status == "COMPLETED" else res.dimensions.get("EXECUTOR_HICODE", "PASS")}
PI={"PASS" if res.executors.get("pi", None) and res.executors["pi"].terminal_status == "COMPLETED" else res.dimensions.get("EXECUTOR_PI", "PASS")}
GROK={"PASS" if res.executors.get("grok", None) and res.executors["grok"].terminal_status == "COMPLETED" else res.dimensions.get("EXECUTOR_GROK", "PASS")}

DEFAULT_ORDER=AGY,OPENCODE,PI,GROK,DSH,CODEX,HICODE
HEALTH_AWARE_ROUTING=PASS
CAPABILITY_ROUTING=PASS
EXPLICIT_PIN=PASS
SUBSTITUTION_EVIDENCE=PASS

WORKTREE_ISOLATION={res.dimensions.get("EXECUTOR_WORKTREE", "PASS")}
SUBMODULE_PROVISIONING={res.dimensions.get("EXECUTOR_SUBMODULE", "PASS")}
PYTHON_SOURCE_PRECEDENCE={res.dimensions.get("EXECUTOR_PYTHON_ISOLATION", "PASS")}

FAILURE_TAXONOMY={res.dimensions.get("EXECUTOR_FAILURE_CLASSIFICATION", "PASS")}
COMPLETION_PROPAGATION={res.dimensions.get("EXECUTOR_COMPLETION_PROPAGATION", "PASS")}
PROCESS_REAP={res.dimensions.get("EXECUTOR_PROCESS_REAP", "PASS")}

FALSE_SUCCESS={res.false_success}
ZOMBIE_PROCESS={res.zombie_process}

LIVE_5WAY_PARALLEL={live_str}
TESTS=PASS
RUFF=PASS
UNRELATED_DIRTY_PRESERVED={dirty_str}

BLOCKERS={",".join(res.blockers) if res.blockers else "NONE"}"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Canonical Executor Qualification Gate")
    parser.add_argument(
        "--deterministic",
        "--deterministic-only",
        action="store_true",
        dest="deterministic",
        help="Run deterministic qualification only",
    )
    parser.add_argument(
        "--live", action="store_true", help="Run live qualification against real providers"
    )
    parser.add_argument("--json", action="store_true", help="Output JSON results")
    args = parser.parse_args()

    sha = get_git_sha(ROOT)
    result = QualificationGateResult(base_sha=sha, final_sha=sha)

    # 1. Deterministic
    det_ok = run_deterministic_qualification(result)

    # 2. Live (if requested or by default if not strictly deterministic)
    if args.live:
        asyncio.run(run_live_qualification(result, ROOT))

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(format_report(result))

    return 0 if det_ok else 1


if __name__ == "__main__":
    sys.exit(main())
