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

OBJECTIVE / COMMAND SEPARATION (integration 20261014, blocking fix):

    A Bot objective is *semantic text*. It is handed to the admitted executor /
    the LLM and never to a shell. Shell execution happens only when the caller
    names a real shell command, and then only through the existing command
    authority (``run_direct_command`` -> ``CommandRunner`` -> ActionGateway /
    PolicyResolver), which owns risk classification and approval. Nothing here
    adds a second approval, scheduler or registry.

    ``local2`` is a native substrate, never an admitted executor. When the
    objective is prose the run is dispatched on the first admitted, selectable
    executor, so a container without ``codex`` no longer refuses every goal.
"""

from __future__ import annotations

import logging
import shlex
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
from veya.remote.executor_registry import get_executor_registry, is_retired_executor

logger = logging.getLogger("veya.bot_runtime.execution")

#: The native substrate that would execute a shell command. It is a substrate,
#: never an admitted executor (ExecutorRegistry stays the admission authority).
NATIVE_SUBSTRATE = "local2"


class ObjectiveNotExecutable(ValueError):
    """A prose objective cannot be shell-executed and no executor accepted it.

    Raised instead of silently running the objective as a shell command: the
    caller gets an actionable error naming the executors that were tried.
    """


def looks_like_shell_command(text: str) -> bool:
    """True only for text that parses as an intentional shell command.

    The first token must be executable-shaped and the parse must consume the
    whole string. Natural language / prose is never a shell command.
    """
    raw = str(text or "").strip()
    if not raw or chr(10) in raw:
        return False
    if any(ch in raw for ch in "，。！？；：、"):
        return False
    if any("一" <= ch <= "鿿" for ch in raw):
        return False
    try:
        argv = shlex.split(raw)
    except ValueError:
        return False
    if not argv:
        return False
    head = argv[0].lower()
    if len(argv) == 1:
        return head in {"true", "false", "pwd"}
    if "/" in head or head.startswith("."):
        return False
    known_commands = {
        "echo",
        "false",
        "true",
        "sh",
        "bash",
        "python",
        "python3",
        "pytest",
        "cat",
        "ls",
        "pwd",
        "mkdir",
        "rm",
        "cp",
        "mv",
        "git",
        "touch",
        "curl",
        "chmod",
        "kill",
        "grep",
        "sed",
        "awk",
        "head",
        "tail",
        "env",
        "export",
        "sleep",
        "which",
    }
    return head in known_commands


@dataclass(frozen=True)
class ExecutionRoute:
    """How one objective is allowed to execute.

    ``native`` is True only for a genuine shell command, which is the single
    case where the physical substrate may be the native one. ``executor`` names
    the admitted executor for a semantic objective (``""`` when none is
    selectable), and ``tried`` records the candidates that were rejected.
    """

    native: bool
    executor: str
    tried: tuple[str, ...] = ()


def resolve_execution_route(objective: str, *, executor: str = "") -> ExecutionRoute:
    """Decide how one objective may execute, without executing anything.

    A prose objective is never a shell command: it is routed to an admitted
    executor. A genuine shell command may run on the native substrate, and it
    still goes through the command authority that owns policy and approval.
    """
    text = str(objective or "").strip()
    if not text:
        raise ValueError("objective must not be empty")
    routed_executor = str(executor or "").strip()
    if looks_like_shell_command(text):
        return ExecutionRoute(native=True, executor=routed_executor or NATIVE_SUBSTRATE)
    if routed_executor and routed_executor != NATIVE_SUBSTRATE:
        # The caller already named an admitted executor; keep it.
        return ExecutionRoute(native=False, executor=routed_executor)
    chosen = select_admission_executor(routed_executor)
    tried = tuple(eid for eid in get_executor_registry().ordered_ids() if is_retired_executor(eid))
    return ExecutionRoute(native=False, executor=chosen, tried=tried)


def select_admission_executor(preferred: str = "") -> str:
    """First admitted, selectable, non-retired executor (registry authority).

    The registry is the only admission authority, so this reads its ordered
    snapshot instead of hardcoding ``codex``. ``preferred`` wins when it is
    itself admitted, selectable and not retired. Returns ``""`` when nothing
    qualifies, which the caller turns into a clear error.
    """
    registry = get_executor_registry()
    admitted = registry.snapshot()
    wanted = str(preferred or "").strip()
    if wanted and wanted in admitted:
        identity = admitted[wanted]
        if identity.selectable and not is_retired_executor(wanted):
            return wanted
    for executor_id in registry.ordered_ids():
        if is_retired_executor(executor_id):
            continue
        if admitted[executor_id].selectable:
            return executor_id
    return ""


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

    # OBJECTIVE / COMMAND ROUTING (blocking fix).
    #
    # The objective is semantic text by default and must never reach a shell.
    # Only an explicit shell-shaped command is allowed onto the native
    # substrate, and that substrate still runs it through the existing command
    # authority (``Local2ExecutorAdapter`` -> ``run_direct_command`` ->
    # CommandRunner -> ActionGateway/PolicyResolver), so classification and
    # approval stay where they already are.
    routing = resolve_execution_route(objective, executor=executor)

    # A prose objective is dispatched to an ADMITTED executor (never local2),
    # so a container without codex falls back to whatever is selectable instead
    # of refusing every goal.
    admission_executor = admission_executor or routing.executor
    if not admission_executor:
        raise ObjectiveNotExecutable(
            f"no admitted executor is selectable for a semantic objective "
            f"(tried: {', '.join(routing.tried) or 'none'}); install or enable one"
        )

    substrate = executor if routing.native else admission_executor
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
    bind_execution(pre, execution_id, project_root, substrate_id=substrate)

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
        substrate=substrate,
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
    "ExecutionRoute",
    "ObjectiveNotExecutable",
    "execute_bot_run",
    "looks_like_shell_command",
    "read_execution_stdout",
    "resolve_execution_route",
    "select_admission_executor",
    "write_bot_memory",
]
