"""Shared Bot execution chain — the single Bot entry into the durable plane.

B3 / decision C, migrated to the canonical ABI in C3 Step 3.
``BotRuntime.submit()`` and ``BotChatService.chat`` both run their objective
through THIS function so there is exactly one Bot-side execution path:

    create_pending_run(bot_id=...)      canonical GoalRun, bot-scoped
      -> bind_execution()               ExecutionAdmission + ExecutorBinding
      -> CanonicalExecutionPlane        single execution ABI (adapter selection)
      -> existing execution substrate   Local2 / admitted executor
      -> ActivityRecorder               unified evidence stream
      -> ExecutionReceipt               durable receipt

AUTHORITY BOUNDARY (frozen):

    GoalRun              = durable execution authority (the only one)
    ExecutionAdmission   = admission authority
    ExecutionControl     = execution control authority
    CanonicalExecutionPlane = dispatch/ABI (no state, no completion decision)
    Bot                  = identity / owner (zero execution authority)

This module therefore owns **no** GoalRun state. It never constructs a
``GoalRunState``; ``create_pending_run`` does. Bot-scoped execution state lives
in the GoalRun (``state.bot_id``), never in a Bot-local store.

TRANSITIONAL (decision C): this uses the dispatch seam
(``create_pending_run`` + ``ExecutionControl``), which is the proven bot-scoped
chain. Decision C2 unifies dispatch/planner/semantic finalization, after which
this delegates to ``project_run_goal(bot_id=...)``. This module is not the
final architecture.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from server.goal_run.execution_abi import (
    CanonicalExecutionPlane,
    CanonicalGoalExecutionRequest,
    ExecutionBindingRef,
)
from server.goal_run.execution_evidence import (
    latest_receipt,
    load_binding_record,
)
from server.goal_run.execution_evidence import (
    read_execution_stdout as _read_execution_stdout,
)
from server.goal_run.models import GoalStatus
from server.goal_run.pre_admission import bind_execution, create_pending_run
from server.goal_run.store import load_goal_run

logger = logging.getLogger("veya.bot_runtime.execution")


@dataclass
class BotExecutionOutcome:
    """Evidence of one real Bot execution (no Bot-local state)."""

    bot_id: str
    goal_run_id: str
    execution_id: str
    project_root: str
    goal_status: GoalStatus
    stdout: str
    reason: str = ""
    receipt: dict[str, Any] = field(default_factory=dict)
    activity_sequence: int = 0
    memory_id: str | None = None

    @property
    def status(self) -> str:
        return self.goal_status.value

    @property
    def succeeded(self) -> bool:
        return self.goal_status is GoalStatus.completed

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "goal_run_id": self.goal_run_id,
            "execution_id": self.execution_id,
            "project_root": self.project_root,
            "goal_status": self.goal_status.value,
            "stdout": self.stdout,
            "reason": self.reason,
            "receipt": self.receipt,
            "activity_sequence": self.activity_sequence,
            "memory_id": self.memory_id,
        }


def _default_memory_controller() -> Any | None:
    try:
        from server.memory_controller import MemoryController

        return MemoryController()
    except Exception:  # pragma: no cover - memory is evidence, never fatal
        return None


def read_execution_stdout(project_root: str, goal_run_id: str) -> str:
    """Reassemble real executor stdout from the durable activity stream."""
    return _read_execution_stdout(project_root, goal_run_id)


def write_bot_memory(
    *,
    bot_id: str,
    goal_run_id: str,
    content: str,
    memory: Any | None = None,
) -> str | None:
    """Write one long-term, bot-scoped memory record (best-effort evidence)."""
    controller = memory if memory is not None else _default_memory_controller()
    if controller is None:
        return None
    try:
        record = controller.observe(
            content,
            type="episodic",
            scope="project",
            scope_id=bot_id,
            provenance=f"bot_execution:{goal_run_id}",
            trust_level="observed",
        )
        memory_id = record.memory_id
        return str(memory_id) if memory_id is not None else None
    except Exception as exc:
        logger.warning("bot memory write failed: %s", exc)
        return None


async def execute_bot_run(
    *,
    project_root: str,
    bot_id: str,
    objective: str,
    session_id: str,
    dispatch_id: str | None = None,
    executor: str = "local2",
    admission_executor: str | None = None,
    timeout_s: float = 600.0,
    memory: Any | None = None,
    memory_content: str | None = None,
) -> BotExecutionOutcome:
    """Run one Bot objective through the canonical execution ABI.

    ``project_root`` must be the bot's own computer workspace
    (``PersistentComputer.workspace_ref``), so execution happens inside the
    bot's private machine and per-bot goal-run state cannot collide.

    C3 Step 3: the Bot dispatch is now the first production caller of
    ``CanonicalExecutionPlane``. The GoalRun, admission, binding and durable
    evidence remain authoritative; this function only creates the canonical
    request and maps the plane's result back to the Bot outcome.

    C2.1 — TWO ROLES, FORMALLY MODELLED. ``admission_executor`` is the ADMITTED
    executor (ExecutorRegistry authority). ``executor`` is the PHYSICAL
    substrate the work runs on (adapter registry). ``local2`` is a genuine
    native substrate and is deliberately NOT admitted as an executor, so the two
    ids differ; admission, binding, activity and receipt each name both roles.
    """
    if not project_root:
        raise ValueError("project_root is required (use the bot computer workspace_ref)")
    if not bot_id:
        raise ValueError("bot_id is required")
    if not objective.strip():
        raise ValueError("objective must not be empty")
    if admission_executor is None:
        admission_executor = executor

    dispatch_id = dispatch_id or f"bot-{uuid.uuid4().hex[:12]}"

    # 1. Canonical, bot-scoped GoalRun. Never hand-built here.
    pre = create_pending_run(
        project_root=project_root,
        dispatch_id=dispatch_id,
        session_id=session_id,
        requested_executor=admission_executor,
        tasks=[{"worker": admission_executor, "task": objective}],
        bot_id=bot_id,
    )
    goal_run_id = pre.goal_run_id

    # 2. ExecutionAdmission + ExecutorBinding (fail-closed before any run).
    execution_id = f"exec-{uuid.uuid4().hex[:12]}"
    bind_execution(pre, execution_id, project_root, substrate_id=executor)

    # 3. Canonical execution ABI. The plane selects the adapter, drives the
    #    existing substrate exactly once, and writes the durable receipt.
    state = load_goal_run(project_root, goal_run_id)
    if state is None:
        raise RuntimeError(f"canonical GoalRun {goal_run_id} disappeared after binding")
    binding_id = str((state.runtime_checkpoint or {}).get("binding_id") or "")
    binding_record = load_binding_record(project_root, binding_id)
    if binding_record is None:
        raise RuntimeError(f"execution binding {binding_id!r} disappeared after binding")

    binding_ref = ExecutionBindingRef.from_record(binding_record)
    request = CanonicalGoalExecutionRequest(
        goal_run_id=goal_run_id,
        task_id=next(iter(state.tasks), ""),
        bot_id=bot_id,
        admitted_executor=admission_executor,
        substrate=executor,
        binding=binding_ref,
        objective=objective,
    )
    result = await CanonicalExecutionPlane().execute(
        request, project_root=project_root, state=state, timeout_s=timeout_s
    )

    # 4. Presentation evidence read back from durable records.
    stdout = read_execution_stdout(project_root, goal_run_id)
    final_state = load_goal_run(project_root, goal_run_id)
    reason = (final_state.last_stop_reason or "") if final_state else ""
    receipt = (
        latest_receipt(
            project_root,
            goal_run_id,
            binding_id=binding_ref.binding_id,
            execution_id=result.execution_id,
        )
        or {}
    )

    # 5. Long-term bot-scoped memory.
    memory_id = None
    if memory_content:
        memory_id = write_bot_memory(
            bot_id=bot_id,
            goal_run_id=goal_run_id,
            content=memory_content,
            memory=memory,
        )

    if result.activity is None:
        raise RuntimeError("substrate execution produced no durable activity reference")
    return BotExecutionOutcome(
        bot_id=bot_id,
        goal_run_id=goal_run_id,
        execution_id=result.execution_id,
        project_root=project_root,
        goal_status=result.status,
        stdout=stdout,
        reason=reason,
        receipt=receipt,
        activity_sequence=result.activity.sequence,
        memory_id=memory_id,
    )


__all__ = [
    "BotExecutionOutcome",
    "execute_bot_run",
    "read_execution_stdout",
    "write_bot_memory",
]
