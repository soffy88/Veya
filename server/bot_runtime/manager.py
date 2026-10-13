"""BotManager — Bot lifecycle application service.

BotManager is the single production authority for Bot lifecycle operations:

    create_bot(), get_bot(), update_bot(), deploy_bot(), activate_bot()
    disable_bot(), retire_bot(), start_bot(), pause_bot(), resume_bot()
    submit_goal(), get_runtime(), get_session()

BotManager does NOT do semantic reasoning. It validates, persists, and
delegates to BotRuntime for execution.

Authority boundaries (frozen, never overridden):
    MasterAgent       = semantic authority
    GoalRun           = durable execution authority
    PermissionEngine  = policy authority
    ComputerSupervisor= physical-computer authority
    IndependentVerifier = acceptance authority
    ExecutorRegistry  = executor capability authority

BotManager is the lifecycle application service. It creates/persists
BotSpec + AgentDefinition + AgentDeployment, validates runtime availability,
and delegates execution to BotRuntime.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.agent_definition import (
    AgentDefinitionStore,
    activate_deployment,
    check_runtime_available,
    create_definition,
    create_deployment,
    disable_deployment,
    retire_deployment,
)
from server.bot_runtime.instance import AgentInstance, AgentInstanceStore
from server.bot_runtime.product import (
    BotComputer,
    BotProduct,
)
from server.bot_runtime.runtime import BotRuntime
from server.goal_run.bot_identity import BotRegistry, BotSpec, validate_bot_spec

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class BotHandle:
    """Result of BotManager operations. Contains all identity refs."""

    bot_id: str
    definition_id: str = ""
    definition_version: int = 0
    deployment_id: str = ""
    deployment_revision: int = 0
    runtime_id: str = ""
    instance_id: str = ""
    status: str = "ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "definition_id": self.definition_id,
            "definition_version": self.definition_version,
            "deployment_id": self.deployment_id,
            "deployment_revision": self.deployment_revision,
            "runtime_id": self.runtime_id,
            "instance_id": self.instance_id,
            "status": self.status,
        }


@dataclass
class BotExecutionResult:
    """Result of BotManager.submit_goal(). Contains execution outcome."""

    bot_id: str
    goal_id: str = ""
    session_id: str = ""
    instance_id: str = ""
    status: str = "unknown"
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)
    rounds: int = 0
    cost_usd: float = 0.0
    error: str = ""
    # Durable-execution evidence (refs + evidence, never execution state).
    execution_id: str = ""
    receipt: dict[str, Any] = field(default_factory=dict)
    activity_sequence: int = 0
    memory_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "goal_id": self.goal_id,
            "session_id": self.session_id,
            "instance_id": self.instance_id,
            "status": self.status,
            "summary": self.summary,
            "artifacts": self.artifacts,
            "rounds": self.rounds,
            "cost_usd": self.cost_usd,
            "error": self.error,
            "execution_id": self.execution_id,
            "receipt": self.receipt,
            "activity_sequence": self.activity_sequence,
            "memory_id": self.memory_id,
        }


class BotManager:
    """Bot lifecycle application service (single production authority).

    BotManager does NOT do semantic reasoning. It validates, persists,
    and delegates to BotRuntime for execution.

    Canonical create_bot() chain:
        validate owner/policy → create BotSpec → create AgentDefinition(v1)
        → create AgentDeployment(r1) → validate runtime availability
        → activate deployment → persist BotSpec binding → return BotHandle
    """

    def __init__(
        self,
        bot_registry: BotRegistry,
        definition_store: AgentDefinitionStore,
        *,
        instance_store: AgentInstanceStore | None = None,
        session_store: Any | None = None,
        llm_fn: Any | None = None,
        tool_pipeline: Any | None = None,
        session_tree: Any | None = None,
        product: BotProduct | None = None,
    ) -> None:
        self._bot_registry = bot_registry
        self._definition_store = definition_store
        self._instance_store = instance_store or AgentInstanceStore()
        self._session_store = session_store
        self._llm_fn = llm_fn
        self._tool_pipeline = tool_pipeline
        self._session_tree = session_tree
        self._product = product
        #: Computer facade over the sole authority (PersistentComputerStore).
        #: Created on demand so callers that pass no BotProduct still get the
        #: authority-backed computer rather than a second store.
        self._computers = product.computers if product is not None else BotProduct().computers
        self._runtimes: dict[str, BotRuntime] = {}

    # ── Bot lifecycle ──────────────────────────────────────────────

    def create_bot(
        self,
        *,
        bot_id: str,
        owner_id: str,
        runtime_id: str,
        name: str = "",
        description: str = "",
        instruction_refs: tuple[str, ...] = (),
        capability_refs: tuple[str, ...] = (),
        skill_refs: tuple[str, ...] = (),
        tool_refs: tuple[str, ...] = (),
        policy_refs: tuple[str, ...] = (),
        available_skill_ids: list[str] | None = None,
        available_playbook_ids: list[str] | None = None,
        workspace_root: str = "",
    ) -> BotHandle:
        """Create a new Bot with full identity chain.

        Canonical chain:
            1. Validate owner / policy
            2. Create BotSpec
            3. Create AgentDefinition(version=1)
            4. Create AgentDeployment(revision=1)
            5. Validate runtime availability
            6. Activate deployment
            7. Persist BotSpec binding
            8. Return BotHandle
        """
        # 1. Validate owner
        if not bot_id.strip():
            raise ValueError("bot_id is required")
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        if not runtime_id.strip():
            raise ValueError("runtime_id is required")

        # Check for existing bot
        existing = self._bot_registry.get(bot_id)
        if existing is not None and existing.has_deployment:
            raise ValueError(f"bot {bot_id!r} already exists with active deployment")

        # 2. Create AgentDefinition
        definition_id = _new_id("def")
        create_definition(
            self._definition_store,
            definition_id=definition_id,
            name=name or f"Bot {bot_id}",
            version=1,
            instruction_refs=instruction_refs,
            capability_refs=capability_refs,
            skill_refs=skill_refs,
            tool_refs=tool_refs,
            policy_refs=policy_refs,
            metadata={"bot_id": bot_id, "owner_id": owner_id},
        )

        # 3. Create AgentDeployment
        deployment_id = _new_id("dep")
        create_deployment(
            self._definition_store,
            deployment_id=deployment_id,
            definition_id=definition_id,
            definition_version=1,
            runtime_id=runtime_id,
            revision=1,
            policy_refs=policy_refs,
        )

        # 4. Validate runtime availability
        runtime_check = check_runtime_available(runtime_id)
        if not runtime_check["available"]:
            raise ValueError(f"runtime unavailable: {runtime_check.get('reason', 'unknown')}")

        # 5. Activate deployment
        activate_deployment(self._definition_store, deployment_id)

        # 6. Create/update BotSpec with full binding
        spec = BotSpec(
            bot_id=bot_id,
            owner_id=owner_id,
            definition_id=definition_id,
            definition_version=1,
            deployment_id=deployment_id,
            deployment_revision=1,
            runtime_id=runtime_id,
            available_skill_ids=available_skill_ids or list(skill_refs),
            available_playbook_ids=available_playbook_ids or [],
            default_workspace_id=workspace_root,
        )

        # Validate and register
        errors = validate_bot_spec(spec)
        if errors:
            raise ValueError(f"invalid bot {bot_id!r}: " + "; ".join(errors))
        self._bot_registry.register(spec)

        # B2: provision the bot's private persistent computer in the sole
        # authority (PersistentComputerStore) and bind its id into the BotSpec
        # (identity binding only — no execution authority).
        computer = self._provision_computer(spec)
        import dataclasses

        spec = dataclasses.replace(spec, computer_id=computer.computer_id)
        self._bot_registry.register(spec)

        logger.info(
            "BotManager created bot=%s definition=%s deployment=%s runtime=%s",
            bot_id,
            definition_id,
            deployment_id,
            runtime_id,
        )

        # 7. Return BotHandle
        return BotHandle(
            bot_id=bot_id,
            definition_id=definition_id,
            definition_version=1,
            deployment_id=deployment_id,
            deployment_revision=1,
            runtime_id=runtime_id,
            status="ready",
        )

    def get_bot(self, bot_id: str) -> BotHandle | None:
        """Get a BotHandle for an existing Bot."""
        spec = self._bot_registry.get(bot_id)
        if spec is None:
            return None
        return BotHandle(
            bot_id=spec.bot_id,
            definition_id=spec.definition_id,
            definition_version=spec.definition_version,
            deployment_id=spec.deployment_id,
            deployment_revision=spec.deployment_revision,
            runtime_id=spec.runtime_id,
            instance_id=spec.instance_id,
            status="ready",
        )

    def list_bots(self, *, owner_id: str | None = None) -> list[BotHandle]:
        """List all Bots (optionally filtered by owner)."""
        specs = self._bot_registry.list(owner_id=owner_id)
        return [
            BotHandle(
                bot_id=s.bot_id,
                definition_id=s.definition_id,
                definition_version=s.definition_version,
                deployment_id=s.deployment_id,
                deployment_revision=s.deployment_revision,
                runtime_id=s.runtime_id,
                instance_id=s.instance_id,
                status="ready",
            )
            for s in specs
        ]

    def update_bot(
        self,
        bot_id: str,
        *,
        available_skill_ids: list[str] | None = None,
        available_playbook_ids: list[str] | None = None,
        config_version: int | None = None,
    ) -> BotHandle:
        """Update a Bot's configuration (not its deployment identity)."""
        spec = self._bot_registry.get(bot_id)
        if spec is None:
            raise ValueError(f"unknown bot: {bot_id!r}")

        import dataclasses

        kwargs: dict[str, Any] = {}
        if available_skill_ids is not None:
            kwargs["available_skill_ids"] = available_skill_ids
        if available_playbook_ids is not None:
            kwargs["available_playbook_ids"] = available_playbook_ids
        if config_version is not None:
            kwargs["config_version"] = config_version

        updated = dataclasses.replace(spec, **kwargs)
        errors = validate_bot_spec(updated)
        if errors:
            raise ValueError("invalid bot update: " + "; ".join(errors))
        self._bot_registry.register(updated)

        return BotHandle(
            bot_id=updated.bot_id,
            definition_id=updated.definition_id,
            definition_version=updated.definition_version,
            deployment_id=updated.deployment_id,
            deployment_revision=updated.deployment_revision,
            runtime_id=updated.runtime_id,
            instance_id=updated.instance_id,
            status="ready",
        )

    def deploy_bot(
        self,
        bot_id: str,
        *,
        runtime_id: str | None = None,
        revision: int = 2,
    ) -> BotHandle:
        """Create a new deployment revision for an existing Bot."""
        spec = self._bot_registry.get(bot_id)
        if spec is None:
            raise ValueError(f"unknown bot: {bot_id!r}")
        if not spec.has_definition:
            raise ValueError(f"bot {bot_id!r} has no definition binding")

        new_runtime_id = runtime_id or spec.runtime_id
        if not new_runtime_id:
            raise ValueError("runtime_id is required")

        # Validate runtime
        runtime_check = check_runtime_available(new_runtime_id)
        if not runtime_check["available"]:
            raise ValueError(f"runtime unavailable: {runtime_check.get('reason', 'unknown')}")

        # Create new deployment
        deployment_id = _new_id("dep")
        create_deployment(
            self._definition_store,
            deployment_id=deployment_id,
            definition_id=spec.definition_id,
            definition_version=spec.definition_version,
            runtime_id=new_runtime_id,
            revision=revision,
        )

        # Activate
        activate_deployment(self._definition_store, deployment_id)

        # Update BotSpec
        import dataclasses

        updated = dataclasses.replace(
            spec,
            deployment_id=deployment_id,
            deployment_revision=revision,
            runtime_id=new_runtime_id,
        )
        self._bot_registry.register(updated)

        return BotHandle(
            bot_id=bot_id,
            definition_id=updated.definition_id,
            definition_version=updated.definition_version,
            deployment_id=deployment_id,
            deployment_revision=revision,
            runtime_id=new_runtime_id,
            status="ready",
        )

    def activate_bot(self, bot_id: str) -> BotHandle:
        """Activate the bot's deployment."""
        spec = self._bot_registry.get(bot_id)
        if spec is None:
            raise ValueError(f"unknown bot: {bot_id!r}")
        if not spec.has_deployment:
            raise ValueError(f"bot {bot_id!r} has no deployment")
        activate_deployment(self._definition_store, spec.deployment_id)
        return self.get_bot(bot_id)  # type: ignore[return-value]

    def disable_bot(self, bot_id: str) -> BotHandle:
        """Disable a Bot (kept for audit; active runs unaffected)."""
        spec = self._bot_registry.get(bot_id)
        if spec is None:
            raise ValueError(f"unknown bot: {bot_id!r}")
        if not spec.has_deployment:
            raise ValueError(f"bot {bot_id!r} has no deployment")
        disable_deployment(self._definition_store, spec.deployment_id)
        return self.get_bot(bot_id)  # type: ignore[return-value]

    def retire_bot(self, bot_id: str) -> BotHandle:
        """Retire a Bot permanently (history retained)."""
        spec = self._bot_registry.get(bot_id)
        if spec is None:
            raise ValueError(f"unknown bot: {bot_id!r}")
        if not spec.has_deployment:
            raise ValueError(f"bot {bot_id!r} has no deployment")
        retire_deployment(self._definition_store, spec.deployment_id)
        return self.get_bot(bot_id)  # type: ignore[return-value]

    # ── Runtime lifecycle ──────────────────────────────────────────

    async def start_bot(self, bot_id: str) -> AgentInstance:
        """Start a Bot runtime instance."""
        spec = self._bot_registry.get(bot_id)
        if spec is None:
            raise ValueError(f"unknown bot: {bot_id!r}")
        if not spec.has_definition or not spec.has_deployment or not spec.has_runtime:
            raise ValueError(f"bot {bot_id!r} is not fully bound")

        runtime = BotRuntime(
            spec,
            self._definition_store,
            instance_store=self._instance_store,
            session_store=self._session_store,
            llm_fn=self._llm_fn,
            tool_pipeline=self._tool_pipeline,
            session_tree=self._session_tree,
            computers=self._computers,
        )
        self._runtimes[bot_id] = runtime
        instance = await runtime.start()
        return instance

    async def pause_bot(self, bot_id: str) -> None:
        """Pause a running Bot."""
        runtime = self._get_runtime(bot_id)
        await runtime.pause()

    async def resume_bot(self, bot_id: str) -> None:
        """Resume a paused Bot."""
        runtime = self._get_runtime(bot_id)
        if runtime.status.value == "paused":
            # Resume is handled by submit() with the existing goal
            pass

    async def stop_bot(self, bot_id: str) -> None:
        """Stop a Bot runtime."""
        runtime = self._get_runtime(bot_id)
        await runtime.shutdown()
        del self._runtimes[bot_id]

    def get_runtime(self, bot_id: str) -> BotRuntime | None:
        """Get the active BotRuntime for a bot."""
        return self._runtimes.get(bot_id)

    def get_session(self, bot_id: str) -> str | None:
        """Get the current session_id for a bot's runtime."""
        runtime = self._runtimes.get(bot_id)
        if runtime is None:
            return None
        return getattr(runtime, "_session_id", None)

    # ── Goal submission ────────────────────────────────────────────

    async def submit_goal(
        self,
        bot_id: str,
        objective: str,
        *,
        source: str = "external",
        session_id: str | None = None,
        parent_session_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> BotExecutionResult:
        """Submit a goal to a Bot for execution.

        Canonical chain:
            Master / external Bot entry
                → resolve Bot
                → resolve Deployment
                → admit Runtime
                → create AgentSession
                → create GoalRun
                → stamp AgentRunIdentity
                → AgentEngine.run()
                → GoalRun checkpoint/events
                → finalization
        """
        spec = self._bot_registry.get(bot_id)
        if spec is None:
            raise ValueError(f"unknown bot: {bot_id!r}")
        if not spec.has_definition or not spec.has_deployment or not spec.has_runtime:
            raise ValueError(f"bot {bot_id!r} is not fully bound")

        # Ensure runtime is started
        runtime = self._runtimes.get(bot_id)
        if runtime is None or runtime.status.value == "stopped":
            runtime = BotRuntime(
                spec,
                self._definition_store,
                instance_store=self._instance_store,
                session_store=self._session_store,
                llm_fn=self._llm_fn,
                tool_pipeline=self._tool_pipeline,
                session_tree=self._session_tree,
                computers=self._computers,
            )
            self._runtimes[bot_id] = runtime
            await runtime.start()

        # Submit goal
        result = await runtime.submit(
            objective,
            session_id=session_id,
            context=context,
        )

        return BotExecutionResult(
            bot_id=bot_id,
            goal_id=result.goal_run_id,
            session_id=runtime.get_session_id() or session_id or "",
            instance_id=runtime.instance_id,
            status=result.status,
            summary=result.summary,
            artifacts=result.artifacts,
            rounds=result.rounds,
            cost_usd=result.cost_usd,
            error=result.error,
            execution_id=result.execution_id,
            receipt=result.receipt,
            activity_sequence=result.activity_sequence,
            memory_id=result.memory_id,
        )

    def _get_runtime(self, bot_id: str) -> BotRuntime:
        """Get the active BotRuntime or raise."""
        runtime = self._runtimes.get(bot_id)
        if runtime is None:
            raise ValueError(f"no active runtime for bot: {bot_id!r}")
        return runtime

    # ── Bot computer (B2) ──────────────────────────────────────────

    def _provision_computer(self, spec: BotSpec) -> BotComputer:
        """Provision (or return the existing) private computer for a bot.

        AUTHORITY: the computer record lives in the sole authority
        (``PersistentComputerStore``); this method only idempotently creates it
        and returns the Bot-facing projection. A restart reuses the same
        computer (same id, same workspace) rather than resetting it.
        """
        workspace_root = spec.default_workspace_id or str(
            Path.home() / ".veya" / "bots" / spec.bot_id / "workspace"
        )
        persistent = self._computers.ensure(
            bot_id=spec.bot_id,
            owner_id=spec.owner_id or spec.bot_id,
            workspace_ref=workspace_root,
            browser_profile_ref=f"profile:{spec.bot_id}",
            computer_id=spec.computer_id or None,
        )
        return BotComputer.from_persistent(persistent, runtime_id=spec.runtime_id)

    def get_computer(self, bot_id: str) -> BotComputer | None:
        """Return the bot's persistent computer (projection only)."""
        return self._computers.get(bot_id)

    def ensure_computer(self, bot_id: str) -> BotComputer | None:
        """Provision the computer if missing (restart-safe)."""
        spec = self._bot_registry.get(bot_id)
        if spec is None:
            return None
        return self._provision_computer(spec)

    def takeover_computer(self, bot_id: str) -> BotComputer | None:
        """Mark the computer as user-controlled (bot must pause conflicting work).

        Takeover maps to the authoritative ``stopped`` lifecycle state: the bot
        no longer drives the machine until release.
        """
        self.ensure_computer(bot_id)
        computer = self._computers.get(bot_id)
        if computer is None:
            return None
        persistent = self._computers.store.set_state(computer.computer_id, "stopped")
        return BotComputer.from_persistent(persistent, runtime_id=computer.runtime_id)

    def release_computer(self, bot_id: str) -> BotComputer | None:
        """Return the computer to the bot after user takeover ends."""
        self.ensure_computer(bot_id)
        computer = self._computers.get(bot_id)
        if computer is None:
            return None
        persistent = self._computers.store.set_state(computer.computer_id, "running")
        return BotComputer.from_persistent(persistent, runtime_id=computer.runtime_id)


__all__ = [
    "BotExecutionResult",
    "BotHandle",
    "BotManager",
]
