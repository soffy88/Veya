"""Qualification-only uvicorn launcher with a checkpoint barrier.

The barrier is process-local test control. It does not add an API route or
change production code; all task data still enters through the HTTP route.
"""

from __future__ import annotations

import fcntl
import inspect
import json
import os
import time
from pathlib import Path

import uvicorn

_startup_lock = None


def acquire_startup_lock() -> None:
    global _startup_lock
    lock_path = os.environ.get("VEYA_QUAL_STARTUP_LOCK")
    if not lock_path:
        return
    _startup_lock = Path(lock_path).open("a+", encoding="utf-8")  # noqa: SIM115
    fcntl.flock(_startup_lock.fileno(), fcntl.LOCK_EX)


acquire_startup_lock()


def append_qualification_event(event: dict[str, object]) -> None:
    runtime_events_path = os.environ.get("VEYA_QUAL_RUNTIME_EVENTS")
    if not runtime_events_path:
        return
    with Path(runtime_events_path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def install_checkpoint_barrier() -> None:
    ready_path = os.environ.get("VEYA_QUAL_CHECKPOINT_READY")
    release_path = os.environ.get("VEYA_QUAL_CHECKPOINT_RELEASE")
    barrier_phase = os.environ.get("VEYA_QUAL_BARRIER_PHASE", "checkpoint1")
    lease_ready_path = os.environ.get("VEYA_QUAL_LEASE_READY")
    if not ready_path or not release_path:
        return

    from veya.platform import load as platform_load

    platform_load("obase")
    from runtime.execution.durable import DurableExecutionRepository
    from server.goal_run.canonical_worker import CanonicalWorkerAdapter

    original = CanonicalWorkerAdapter.checkpoint

    def checkpoint(self, state, project_root, *, reason="goal_run"):
        result = original(self, state, project_root, reason=reason)
        append_qualification_event(
            {
                "event": "qualification.checkpoint",
                "goal_run_id": state.goal_id,
                "reason": reason,
                "pid": os.getpid(),
                "ts": time.time(),
            }
        )
        should_block = (
            barrier_phase == "checkpoint1" and reason == "before_first_action"
        ) or (barrier_phase == "checkpoint2" and reason == "task_round")
        if should_block:
            Path(ready_path).write_text(
                f"{state.goal_id}\n", encoding="utf-8"
            )
            while not Path(release_path).exists():
                time.sleep(0.05)
        return result

    CanonicalWorkerAdapter.checkpoint = checkpoint

    if lease_ready_path:
        original_start = DurableExecutionRepository.start

        async def start(self, claim):
            result = await original_start(self, claim)
            marker = {
                "event": "qualification.lease_acquired",
                "goal_run_id": claim.goal_run_id,
                "work_item_id": claim.work_item_id,
                "kind": claim.kind,
                "worker_id": claim.worker_id,
                "lease_token": claim.lease_token,
                "attempt_id": claim.attempt_id,
                "pid": os.getpid(),
                "ts": time.time(),
            }
            with Path(lease_ready_path).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(marker, sort_keys=True) + "\n")
            append_qualification_event(marker)
            return result

        DurableExecutionRepository.start = start


def install_one_shot_execution_failure() -> None:
    """Arm a process-local failure at the existing gateway boundary."""
    if os.environ.get("VEYA_QUAL_INJECT_FAILURE", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return

    from server.action_gateway_adapter import ActionGatewayAdapter

    original_execute = ActionGatewayAdapter.execute
    state = {"used": False}

    async def execute(self, name, kwargs, executor, *args, **call_kwargs):
        if (
            not state["used"]
            and call_kwargs.get("source") == "goal_run_canonical_action"
        ):

            async def fail_once(**arguments):
                if not state["used"]:
                    state["used"] = True
                    append_qualification_event(
                        {
                            "event": "qualification.injected_failure",
                            "action": name,
                            "pid": os.getpid(),
                            "ts": time.time(),
                        }
                    )
                    raise RuntimeError("qualification one-shot canonical execution failure")
                result = executor(**arguments)
                if inspect.isawaitable(result):
                    return await result
                return result

            executor = fail_once
        return await original_execute(
            self, name, kwargs, executor, *args, **call_kwargs
        )

    ActionGatewayAdapter.execute = execute


def install_deterministic_qualification_model() -> None:
    """Use the existing local test model seam; never alter production code."""
    from server.coordinator_master import master_coordinator

    failure_mode = os.environ.get("VEYA_QUAL_INJECT_FAILURE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    decision_count = 0

    async def respond(messages, **_kwargs):
        nonlocal decision_count
        decision_count += 1
        has_failure_evidence = any(
            isinstance(message, dict)
            and (
                "REAL FAILURE EVIDENCE from this task"
                in str(message.get("content") or "")
                or (
                    message.get("role") == "tool"
                    and '"status": "failed"' in str(message.get("content") or "")
                    and "failure_evidence" in str(message.get("content") or "")
                )
            )
            for message in messages
        )
        if failure_mode and has_failure_evidence:
            # Use the same available read-only capability with a distinct
            # normalized path.  The first action is intentionally failed by
            # the out-of-band injector; the second is still selected by this
            # model seam from the observed failure result and is not injected
            # by the harness as an execution result.
            tool = "list_files"
            arguments = '{"path":"./"}'
            call_id = "qual-read-recovered"
        elif failure_mode:
            tool = "read_file"
            arguments = '{"path":"missing-before-replan.txt"}'
            call_id = "qual-read-failing"
        else:
            tool = "list_files"
            arguments = '{"path":"."}'
            call_id = "qual-read-1"
        append_qualification_event(
            {
                "event": "qualification.model_decision",
                "call_id": call_id,
                "decision_count": decision_count,
                "tool": tool,
                "ts": time.time(),
            }
        )
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": tool,
                                    "arguments": arguments,
                                },
                            }
                        ],
                    }
                }
            ]
        }

    master_coordinator._agent._llm_caller = respond


install_checkpoint_barrier()
install_deterministic_qualification_model()
install_one_shot_execution_failure()

uvicorn.run(
    "server.app:app",
    host=os.environ.get("VEYA_QUAL_HOST", "127.0.0.1"),
    port=int(os.environ.get("VEYA_QUAL_PORT", "18766")),
    log_level="warning",
)
