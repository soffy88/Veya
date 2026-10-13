"""BotRuntime — Bot-local runtime assembly / lifecycle boundary.

BotRuntime assembles the Bot product chain into one executable instance:

    BotSpec + AgentDefinition + AgentDeployment + RuntimeResolution
    + AgentInstance + AgentSession + Memory + Tool Surface + GoalRun
    → AgentEngine.run()

Authority boundaries (frozen, never overridden):
    MasterAgent       = semantic authority
    GoalRun           = durable execution authority
    PermissionEngine  = policy authority
    ComputerSupervisor= physical-computer authority
    IndependentVerifier = acceptance authority
    ExecutorRegistry  = executor capability authority

BotRuntime does NOT:
    - interpret user intent globally
    - replace MasterAgent
    - create second scheduler / task store / acceptance system
    - directly execute physical tools (must go through ActionGateway)
    - modify GoalRun terminal state (bypasses GoalRun authority)

BotRuntime is the caller / binding layer. All durable execution state
(plan, task graph, checkpoint, resume, recovery, finalize, artifact,
event) remains owned by the existing GoalRun authority.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.bot_scope import CrossBotAccessDenied
from server.agent_definition import (
    AgentDefinitionStore,
    RuntimeResolution,
    resolve_agent_deployment,
)
from server.bot_runtime.engine import (
    AgentEngine,
    AgentLoopAdapter,
    EngineConfig,
    EngineResult,
    GenesisAgentAdapter,
)
from server.bot_runtime.execution import execute_bot_run
from server.bot_runtime.instance import (
    AgentInstance,
    AgentInstanceStatus,
    AgentInstanceStore,
)
from server.bot_runtime.product import BotComputer
from server.goal_run.bot_identity import BotSpec
from server.goal_run.models import GoalStatus
from server.goal_run.store import load_goal_run

logger = logging.getLogger(__name__)

# Runtime ID to engine adapter mapping
_RUNTIME_ADAPTERS: dict[str, type] = {
    "genesis": GenesisAgentAdapter,
    "agent_loop": AgentLoopAdapter,
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class BotRuntimeError(Exception):
    """Raised when a BotRuntime operation fails fail-closed."""

    def __init__(self, reason: str, *, bot_id: str = ""):
        super().__init__(f"BotRuntime error for bot {bot_id!r}: {reason}")
        self.reason = reason
        self.bot_id = bot_id


@dataclass
class BotRuntimeSnapshot:
    """Immutable snapshot of BotRuntime state."""

    bot_id: str
    instance_id: str
    status: str
    goal_id: str | None = None
    session_id: str | None = None
    definition_id: str = ""
    definition_version: int = 0
    deployment_id: str = ""
    deployment_revision: int = 0
    runtime_id: str = ""
    snapshot_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "instance_id": self.instance_id,
            "status": self.status,
            "goal_id": self.goal_id,
            "session_id": self.session_id,
            "definition_id": self.definition_id,
            "definition_version": self.definition_version,
            "deployment_id": self.deployment_id,
            "deployment_revision": self.deployment_revision,
            "runtime_id": self.runtime_id,
            "snapshot_at": self.snapshot_at,
        }


class BotRuntime:
    """Bot-local runtime assembly / lifecycle boundary.

    Assembles BotSpec + AgentDefinition + AgentDeployment + RuntimeResolution
    + AgentInstance + AgentSession + Memory + Tool Surface + GoalRun into
    one executable Bot instance.

    Lifecycle:
        CREATED → ADMITTED → READY → RUNNING → PAUSED → STOPPED → FAILED

    Recovery:
        STOPPED/FAILED → ADMITTED → READY → RUNNING

    Does NOT create second execution store, scheduler, or acceptance system.
    """

    def __init__(
        self,
        bot_spec: BotSpec,
        definition_store: AgentDefinitionStore,
        *,
        instance_store: AgentInstanceStore | None = None,
        session_store: Any | None = None,
        computers: Any | None = None,
        memory: Any | None = None,
        executor: str = "local2",
        admission_executor: str = "codex",
        llm_fn: Any | None = None,
        tool_pipeline: Any | None = None,
        session_tree: Any | None = None,
        max_consecutive_errors: int = 3,
    ) -> None:
        self._spec = bot_spec
        self._definition_store = definition_store
        self._instance_store = instance_store or AgentInstanceStore()
        self._session_store = session_store
        self._memory = memory
        self._executor = executor
        self._admission_executor = admission_executor
        self._llm_fn = llm_fn
        self._tool_pipeline = tool_pipeline
        self._session_tree = session_tree
        #: Bot computer facade over the sole authority (PersistentComputerStore).
        self._computers = computers
        self._max_errors = max_consecutive_errors

        self._instance: AgentInstance | None = None
        self._engine: AgentEngine | None = None
        self._resolution: RuntimeResolution | None = None
        self._session_id: str | None = None
        self._goal_id: str | None = None
        self._status: AgentInstanceStatus = AgentInstanceStatus.CREATED
        self._stopping = False
        self._computer: Any | None = None

    @property
    def bot_id(self) -> str:
        return self._spec.bot_id

    @property
    def instance_id(self) -> str:
        return self._instance.instance_id if self._instance else ""

    @property
    def status(self) -> AgentInstanceStatus:
        return self._status

    def get_session_id(self) -> str | None:
        """Current session id bound to this runtime instance."""
        return self._session_id

    @property
    def computer(self) -> Any:
        """The bot's computer projection bound to this runtime."""
        return self._computer

    def _bind_computer(self) -> Any:
        """Resolve the bot's computer from the sole authority, fail-closed.

        The computer is a real execution input, not metadata: its
        ``workspace_ref`` is the workspace root the agent loop executes in.
        A missing or terminated computer refuses to run.
        """
        if self._computers is None:
            raise BotRuntimeError("no computer authority bound", bot_id=self._spec.bot_id)

        if self._spec.computer_id:
            persistent = self._computers.get_persistent(
                self._spec.computer_id, bot_id=self._spec.bot_id
            )
        else:
            persistent = self._computers.ensure(
                bot_id=self._spec.bot_id,
                owner_id=self._spec.owner_id or self._spec.bot_id,
                workspace_ref=self._spec.default_workspace_id
                or str(Path.home() / ".veya" / "bots" / self._spec.bot_id / "workspace"),
                browser_profile_ref=f"profile:{self._spec.bot_id}",
            )

        if persistent is None:
            raise BotRuntimeError("bot has no persistent computer", bot_id=self._spec.bot_id)
        if persistent.lifecycle_state in ("terminated", "failed"):
            raise BotRuntimeError(
                f"computer {persistent.computer_id} is {persistent.lifecycle_state}",
                bot_id=self._spec.bot_id,
            )
        if not persistent.workspace_ref:
            raise BotRuntimeError(
                f"computer {persistent.computer_id} has no workspace_ref",
                bot_id=self._spec.bot_id,
            )

        # The runtime owns the machine once admitted: transition the authority
        # record first, then project, so the Bot view is never stale.
        persistent = self._computers.store.set_state(persistent.computer_id, "running")
        self._computer = BotComputer.from_persistent(persistent, runtime_id=self._spec.runtime_id)
        return persistent

    async def start(self) -> AgentInstance:
        """Start the BotRuntime: admit, resolve, instantiate, ready.

        Chain:
            1. Validate BotSpec has definition + deployment + runtime binding
            2. Resolve AgentDeployment → RuntimeResolution
            3. Check runtime availability
            4. Create AgentInstance
            5. Instantiate AgentEngine adapter
            6. Transition: CREATED → ADMITTED → READY
        """
        if self._status not in (
            AgentInstanceStatus.CREATED,
            AgentInstanceStatus.STOPPED,
            AgentInstanceStatus.FAILED,
        ):
            raise BotRuntimeError(
                f"cannot start from status {self._status.value}", bot_id=self._spec.bot_id
            )

        # 1. Validate BotSpec bindings
        if not self._spec.has_definition:
            raise BotRuntimeError("BotSpec missing definition_id/version", bot_id=self._spec.bot_id)
        if not self._spec.has_deployment:
            raise BotRuntimeError(
                "BotSpec missing deployment_id/revision", bot_id=self._spec.bot_id
            )
        if not self._spec.has_runtime:
            raise BotRuntimeError("BotSpec missing runtime_id", bot_id=self._spec.bot_id)

        # 2. Resolve deployment → runtime
        try:
            self._resolution = resolve_agent_deployment(
                self._definition_store,
                deployment_id=self._spec.deployment_id,
                revision=self._spec.deployment_revision,
            )
        except ValueError as exc:
            raise BotRuntimeError(str(exc), bot_id=self._spec.bot_id) from exc

        # Validate deployment matches BotSpec
        if self._resolution.definition_id != self._spec.definition_id:
            raise BotRuntimeError(
                f"deployment definition_id mismatch: {self._resolution.definition_id} != {self._spec.definition_id}",
                bot_id=self._spec.bot_id,
            )
        if self._resolution.definition_version != self._spec.definition_version:
            raise BotRuntimeError(
                f"deployment definition_version mismatch: {self._resolution.definition_version} != {self._spec.definition_version}",
                bot_id=self._spec.bot_id,
            )
        if self._resolution.runtime_id != self._spec.runtime_id:
            raise BotRuntimeError(
                f"deployment runtime_id mismatch: {self._resolution.runtime_id} != {self._spec.runtime_id}",
                bot_id=self._spec.bot_id,
            )

        # 3. Check runtime availability (fail-closed)
        if not self._resolution.runtime_available:
            reasons = "; ".join(self._resolution.reasons) or "unknown"
            raise BotRuntimeError(f"runtime unavailable: {reasons}", bot_id=self._spec.bot_id)

        self._status = AgentInstanceStatus.ADMITTED

        # 3b. Bind the bot's persistent computer (real execution input).
        persistent = self._bind_computer()
        logger.info(
            "BotRuntime bound computer=%s workspace=%s",
            persistent.computer_id,
            persistent.workspace_ref,
        )

        # 4. Create AgentInstance
        instance_id = self._spec.instance_id or _new_id("inst")
        self._instance = AgentInstance(
            instance_id=instance_id,
            bot_id=self._spec.bot_id,
            definition_id=self._resolution.definition_id,
            definition_version=self._resolution.definition_version,
            deployment_id=self._resolution.deployment_id,
            deployment_revision=self._resolution.deployment_revision,
            runtime_id=self._resolution.runtime_id,
            status=AgentInstanceStatus.ADMITTED,
        )
        self._instance_store.put(self._instance)

        # 5. Instantiate AgentEngine adapter
        self._engine = self._create_engine()

        # 6. Start engine
        config = EngineConfig(
            bot_id=self._spec.bot_id,
            definition_id=self._resolution.definition_id,
            definition_version=self._resolution.definition_version,
            deployment_id=self._resolution.deployment_id,
            deployment_revision=self._resolution.deployment_revision,
            runtime_id=self._resolution.runtime_id,
            instance_id=instance_id,
            policy_refs=self._resolution.policy_refs,
            config_refs=self._resolution.config_refs,
            workspace_ref=self._resolution.config_refs[0] if self._resolution.config_refs else "",
            computer_id=persistent.computer_id,
            workspace_root=persistent.workspace_ref,
        )
        await self._engine.start(config)

        # Update instance status
        self._instance = AgentInstance(
            instance_id=instance_id,
            bot_id=self._spec.bot_id,
            definition_id=self._resolution.definition_id,
            definition_version=self._resolution.definition_version,
            deployment_id=self._resolution.deployment_id,
            deployment_revision=self._resolution.deployment_revision,
            runtime_id=self._resolution.runtime_id,
            status=AgentInstanceStatus.READY,
            created_at=self._instance.created_at,
        )
        self._instance_store.put(self._instance)
        self._status = AgentInstanceStatus.READY

        logger.info(
            "BotRuntime started: bot=%s instance=%s runtime=%s",
            self._spec.bot_id,
            instance_id,
            self._resolution.runtime_id,
        )
        return self._instance

    def _create_engine(self) -> AgentEngine:
        """Create the AgentEngine adapter from runtime_id."""
        runtime_id = self._spec.runtime_id
        adapter_cls = _RUNTIME_ADAPTERS.get(runtime_id)
        if adapter_cls is None:
            # Default to AgentLoopAdapter for unknown runtimes
            adapter_cls = AgentLoopAdapter

        # Prepare common parameters
        kwargs = {
            "llm_fn": self._llm_fn,
            "tool_pipeline": getattr(self, "_tool_pipeline", None),
            "session_tree": getattr(self, "_session_tree", None),
            "max_consecutive_errors": self._max_errors,
        }

        # GenesisAgentAdapter only accepts llm_fn, filter others
        if adapter_cls is GenesisAgentAdapter:
            kwargs = {"llm_fn": self._llm_fn}

        return adapter_cls(**kwargs)

    async def submit(
        self,
        objective: str,
        *,
        session_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> EngineResult:
        """Submit a goal objective for execution.

        Chain:
            1. Validate runtime is READY
            2. Create AgentSession (if no session_id provided)
            3. Create GoalRun with full identity snapshot
            4. Stamp AgentRunIdentity
            5. AgentEngine.run()
            6. Return EngineResult

        GoalRun authority: BotRuntime creates the GoalRun with all identity
        fields stamped once. After creation, BotRuntime does NOT re-interpret
        identity; all durable execution state stays in GoalRun.
        """
        if self._status is not AgentInstanceStatus.READY:
            raise BotRuntimeError(
                f"cannot submit from status {self._status.value}", bot_id=self._spec.bot_id
            )
        if self._engine is None:
            raise BotRuntimeError("engine not initialised", bot_id=self._spec.bot_id)
        if self._instance is None:
            raise BotRuntimeError("instance not created", bot_id=self._spec.bot_id)

        self._status = AgentInstanceStatus.RUNNING

        # Create session if not provided
        if session_id is None:
            session_id = _new_id("sess")
        self._session_id = session_id

        # Update instance to RUNNING
        self._instance = AgentInstance(
            instance_id=self._instance.instance_id,
            bot_id=self._spec.bot_id,
            definition_id=self._resolution.definition_id,
            definition_version=self._resolution.definition_version,
            deployment_id=self._resolution.deployment_id,
            deployment_revision=self._resolution.deployment_revision,
            runtime_id=self._resolution.runtime_id,
            status=AgentInstanceStatus.RUNNING,
            created_at=self._instance.created_at,
        )
        self._instance_store.put(self._instance)

        # The bot's own computer workspace is the execution root, so per-bot
        # goal-run state can never collide across bots.
        if self._computer is None:
            raise BotRuntimeError("no computer bound", bot_id=self._spec.bot_id)

        # Execute through AgentLoop (the shared Veya agent stack).
        exec_context: dict[str, Any] = dict(context or {})
        exec_context.update(
            {
                "bot_id": self._spec.bot_id,
                "session_id": session_id,
                "agent_instance_id": self._instance.instance_id,
                "agent_definition_id": self._resolution.definition_id,
                "agent_deployment_id": self._resolution.deployment_id,
                "computer_id": self._computer.computer_id,
                "workspace_root": self._computer.workspace_root,
            }
        )

        result = await self._engine.run(objective, context=exec_context)

        # Durable execution: the canonical GoalRun authority owns all execution
        # state. BotRuntime holds none -- it only records the id for retrieval.
        self._goal_id = None
        outcome = await execute_bot_run(
            project_root=self._computer.workspace_root,
            bot_id=self._spec.bot_id,
            objective=objective,
            session_id=session_id,
            executor=self._executor,
            admission_executor=self._admission_executor,
            memory=self._memory,
            memory_content=f"[bot {self._spec.bot_id}] objective: {objective}",
        )
        self._goal_id = outcome.goal_run_id

        # C2.3 FINALIZATION AUTHORITY: the instance status follows the
        # canonical GoalRun, never the agent engine. "LLM says done" is not
        # completion; only the GoalRun terminal state is.
        if outcome.goal_status is GoalStatus.completed:
            self._status = AgentInstanceStatus.STOPPED
        elif outcome.goal_status is GoalStatus.paused:
            self._status = AgentInstanceStatus.PAUSED
        else:
            self._status = AgentInstanceStatus.FAILED

        logger.info(
            "BotRuntime submit finished bot=%s goal_run=%s engine=%s goal=%s",
            self._spec.bot_id,
            outcome.goal_run_id,
            result.status,
            outcome.status,
        )
        result.goal_run_id = outcome.goal_run_id
        result.execution_id = outcome.execution_id
        result.receipt = outcome.receipt
        result.activity_sequence = outcome.activity_sequence
        result.memory_id = outcome.memory_id
        # The reported status is the GoalRun authority's verdict.
        result.status = outcome.status
        if outcome.goal_status is not GoalStatus.completed and outcome.reason:
            result.error = outcome.reason
        if not result.summary:
            result.summary = outcome.stdout or outcome.reason
        return result

    async def resume(self, goal_id: str, checkpoint: dict[str, Any] | None = None) -> EngineResult:
        """Continue an existing GoalRun without creating or owning one.

        BotRuntime does NOT keep GoalRun state: the GoalRun authority owns it.
        This method runs the agent engine on the same session and the bot's own
        computer workspace, then *reads back* the authoritative GoalRun.

        True GoalRun-level resume (``project_run_goal(resume_goal_id=...)``)
        belongs to the semantic path and lands with decision C2 together with
        its ``bot_id`` scoping; until then resume is restricted to a known
        bot-scoped GoalRun and refuses anything else rather than fabricating one.
        """
        if self._engine is None:
            raise BotRuntimeError("engine not initialised", bot_id=self._spec.bot_id)
        if self._instance is None:
            raise BotRuntimeError("instance not created", bot_id=self._spec.bot_id)
        if self._computer is None:
            raise BotRuntimeError("no computer bound", bot_id=self._spec.bot_id)
        if self._status not in (
            AgentInstanceStatus.STOPPED,
            AgentInstanceStatus.FAILED,
            AgentInstanceStatus.PAUSED,
        ):
            raise BotRuntimeError(
                f"cannot resume from status {self._status.value}", bot_id=self._spec.bot_id
            )

        # Transition back into a resumable instance identity.
        self._status = AgentInstanceStatus.RUNNING
        self._instance = AgentInstance(
            instance_id=self._instance.instance_id,
            bot_id=self._spec.bot_id,
            definition_id=self._resolution.definition_id,
            definition_version=self._resolution.definition_version,
            deployment_id=self._resolution.deployment_id,
            deployment_revision=self._resolution.deployment_revision,
            runtime_id=self._resolution.runtime_id,
            status=AgentInstanceStatus.RUNNING,
            created_at=self._instance.created_at,
        )
        self._instance_store.put(self._instance)

        result = await self._engine.resume(checkpoint or {})

        # Read the authoritative GoalRun (never write a Bot-local copy).
        observed = None
        if goal_id:
            observed = load_goal_run(self._computer.workspace_root, goal_id)
        if observed is None:
            self._status = (
                AgentInstanceStatus.STOPPED
                if result.status == "success"
                else AgentInstanceStatus.FAILED
            )
            return result

        if observed.bot_id != self._spec.bot_id:
            raise CrossBotAccessDenied(f"goal_run:{goal_id}", self._spec.bot_id, observed.bot_id)

        self._status = (
            AgentInstanceStatus.STOPPED
            if result.status == "success"
            else AgentInstanceStatus.FAILED
        )
        result.goal_run_id = observed.goal_id
        return result

    async def pause(self) -> None:
        """Pause the running Bot."""
        if self._status is not AgentInstanceStatus.RUNNING:
            raise BotRuntimeError(
                f"cannot pause from status {self._status.value}", bot_id=self._spec.bot_id
            )
        self._status = AgentInstanceStatus.PAUSED
        if self._instance is not None:
            self._instance = AgentInstance(
                instance_id=self._instance.instance_id,
                bot_id=self._spec.bot_id,
                definition_id=self._resolution.definition_id,
                definition_version=self._resolution.definition_version,
                deployment_id=self._resolution.deployment_id,
                deployment_revision=self._resolution.deployment_revision,
                runtime_id=self._resolution.runtime_id,
                status=AgentInstanceStatus.PAUSED,
                created_at=self._instance.created_at,
            )
            self._instance_store.put(self._instance)
        logger.info("BotRuntime paused: bot=%s", self._spec.bot_id)

    async def shutdown(self) -> None:
        """Shutdown the BotRuntime gracefully."""
        if self._engine is not None:
            await self._engine.stop()
        self._status = AgentInstanceStatus.STOPPED
        if self._instance is not None:
            self._instance = AgentInstance(
                instance_id=self._instance.instance_id,
                bot_id=self._spec.bot_id,
                definition_id=self._resolution.definition_id,
                definition_version=self._resolution.definition_version,
                deployment_id=self._resolution.deployment_id,
                deployment_revision=self._resolution.deployment_revision,
                runtime_id=self._resolution.runtime_id,
                status=AgentInstanceStatus.STOPPED,
                created_at=self._instance.created_at,
            )
            self._instance_store.put(self._instance)
        logger.info("BotRuntime shut down: bot=%s", self._spec.bot_id)

    def snapshot(self) -> BotRuntimeSnapshot:
        """Return an immutable snapshot of the runtime state."""
        return BotRuntimeSnapshot(
            bot_id=self._spec.bot_id,
            instance_id=self._instance.instance_id if self._instance else "",
            status=self._status.value,
            goal_id=self._goal_id,
            session_id=self._session_id,
            definition_id=self._spec.definition_id,
            definition_version=self._spec.definition_version,
            deployment_id=self._spec.deployment_id,
            deployment_revision=self._spec.deployment_revision,
            runtime_id=self._spec.runtime_id,
        )


__all__ = [
    "BotRuntime",
    "BotRuntimeError",
    "BotRuntimeSnapshot",
]
