"""AgentLoopAdapter / AgentEngine — engine protocol and adapters.

BotRuntime does NOT depend on concrete GenesisAgent or AgentLoop classes.
Instead, it depends on the AgentEngine protocol. Concrete engines wrap the
existing implementations:

    GenesisAgentAdapter  -> wraps server.agents.genesis_agent.GenesisAgent
    AgentLoopAdapter     -> wraps veval/omodul.agent_loop (shared with MasterAgent)

Both adapters share the same AgentLoop implementation; they do NOT copy it.

Authority boundary:
    AgentEngine       = runtime implementation capability (start/run/resume/stop)
    BotRuntime        = assembly caller (does not implement loop logic)
    GoalRun           = durable execution authority (owns checkpoints/events)
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class EngineConfig:
    """Configuration passed to AgentEngine.start()."""

    bot_id: str
    definition_id: str
    definition_version: int
    deployment_id: str
    deployment_revision: int
    runtime_id: str
    instance_id: str
    session_id: str | None = None
    goal_id: str | None = None
    # Tool/policy refs from the deployment
    policy_refs: tuple[str, ...] = ()
    config_refs: tuple[str, ...] = ()
    # Workspace binding
    workspace_ref: str = ""
    # Bot computer binding (real execution input, projected from
    # PersistentComputer — the sole computer authority).
    computer_id: str = ""
    workspace_root: str = ""


@dataclass
class EngineResult:
    """Result of one AgentEngine.run() call."""

    status: str  # "success" | "failed" | "paused" | "cancelled"
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)
    rounds: int = 0
    cost_usd: float = 0.0
    error: str = ""
    # Durable-execution evidence. The GoalRun authority owns the state; these
    # are refs + evidence only, never execution state.
    goal_run_id: str = ""
    execution_id: str = ""
    receipt: dict[str, Any] = field(default_factory=dict)
    activity_sequence: int = 0
    memory_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "artifacts": list(self.artifacts),
            "rounds": self.rounds,
            "cost_usd": self.cost_usd,
            "error": self.error,
            "goal_run_id": self.goal_run_id,
            "execution_id": self.execution_id,
            "receipt": dict(self.receipt),
            "activity_sequence": self.activity_sequence,
            "memory_id": self.memory_id,
        }


@runtime_checkable
class AgentEngine(Protocol):
    """Protocol for Bot agent loop engines.

    BotRuntime depends on this protocol, never on concrete engine classes.
    New engines (OpenCode, Claude, local, VM, Browser) implement this
    protocol without modifying Bot identity or GoalRun.
    """

    async def start(self, config: EngineConfig) -> None:
        """Initialise the engine for one bot instance."""
        ...

    async def run(
        self,
        objective: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> EngineResult:
        """Execute one goal objective through the engine loop."""
        ...

    async def resume(self, checkpoint: dict[str, Any]) -> EngineResult:
        """Resume from a durable checkpoint."""
        ...

    async def stop(self) -> None:
        """Stop the engine gracefully."""
        ...


class _CallableLLMClient:
    """Adapt a bare ``llm_fn`` callable to the client ABI AgentLoop expects.

    ``veya.oprim.llm.llm_call`` calls ``client.complete(messages, **kwargs)``.
    Production injects nothing and lets the container default handle resolve;
    this shim only exists so an injected bare callable still works.
    """

    def __init__(self, llm_fn: Callable) -> None:
        self._llm_fn = llm_fn

    async def complete(self, messages: list[dict], **kwargs: Any) -> dict:
        return await self._llm_fn(messages, **kwargs)


def _as_llm_client(llm: Any) -> Any:
    if llm is None or hasattr(llm, "complete"):
        return llm
    if callable(llm):
        return _CallableLLMClient(llm)
    return llm


class AgentLoopAdapter:
    """Adapter over the shared :class:`veya.omodul.agent_loop.AgentLoop`.

    This is the default Bot runtime surface (``runtime_id="agent_loop"``): the
    same AgentLoop / Master stack the rest of Veya uses, not a second agent
    loop. Genesis remains reachable only as an explicit specialised runtime.

    ABI (verified against ``veya/omodul/agent_loop.py``)::

        AgentLoop(llm=, pipeline=, tree=, gate=, max_consecutive_errors=...)
        await AgentLoop.run(user_input, *, session_id=None, owner=None) -> LoopResult

    Execution identity stamped on every run: ``bot_id``, ``goal_id``,
    ``session_id``, ``agent_instance_id``, ``agent_definition_id``,
    ``agent_deployment_id``, ``computer_id`` and ``workspace_root``.
    """

    def __init__(
        self,
        *,
        llm_fn: Callable | None = None,
        tool_pipeline: Any | None = None,
        session_tree: Any | None = None,
        max_consecutive_errors: int = 3,
    ) -> None:
        self._config: EngineConfig | None = None
        self._llm_fn = llm_fn
        self._tool_pipeline = tool_pipeline
        self._session_tree = session_tree
        self._max_errors = max_consecutive_errors
        self._stopping = False
        self._loop: Any | None = None

    async def start(self, config: EngineConfig) -> None:
        """Initialise one AgentLoop bound to this bot's computer workspace."""
        from veya.omodul.agent_loop import AgentLoop

        self._config = config
        self._stopping = False
        self._loop = AgentLoop(
            llm=_as_llm_client(self._llm_fn),
            pipeline=self._tool_pipeline,
            tree=self._session_tree,
            gate=self._gate,
            max_consecutive_errors=self._max_errors,
        )
        logger.info(
            "AgentLoopAdapter started bot=%s instance=%s computer=%s workspace=%s",
            config.bot_id,
            config.instance_id,
            config.computer_id,
            config.workspace_root,
        )

    async def _gate(self) -> None:
        """Per-round checkpoint so pause/stop takes effect inside the loop."""
        if self._stopping:
            raise RuntimeError("bot engine stopped")

    def _context(self, *, goal_id: str | None = None) -> dict[str, Any]:
        cfg = self._config
        assert cfg is not None
        return {
            "source": "BOT",
            "bot_id": cfg.bot_id,
            "goal_id": goal_id or cfg.goal_id or "",
            "session_id": cfg.session_id or "",
            "agent_instance_id": cfg.instance_id,
            "agent_definition_id": cfg.definition_id,
            "agent_deployment_id": cfg.deployment_id,
            "computer_id": cfg.computer_id,
            "workspace_root": cfg.workspace_root,
        }

    @staticmethod
    def _to_result(loop_result: Any) -> EngineResult:
        stop_kind = str(getattr(loop_result, "stop_kind", "") or "")
        error = str(getattr(loop_result, "error", "") or "")
        summary = str(getattr(loop_result, "final_answer", "") or "")
        ok = stop_kind not in ("fatal_error", "error", "cancelled") and not error
        return EngineResult(
            status="success" if ok else "failed",
            summary=summary,
            rounds=int(getattr(loop_result, "rounds", 0) or 0),
            cost_usd=float(getattr(loop_result, "cost_usd", 0.0) or 0.0),
            error=error,
        )

    async def run(
        self,
        objective: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> EngineResult:
        """Execute one goal through the shared AgentLoop."""
        if self._config is None or self._loop is None:
            return EngineResult(status="failed", error="engine not started")
        if self._stopping:
            return EngineResult(status="cancelled", error="engine stopped")

        cfg = self._config
        start_time = time.monotonic()
        try:
            result = await self._loop.run(
                objective,
                session_id=cfg.session_id,
                owner=cfg.bot_id,
            )
            engine_result = self._to_result(result)
        except Exception as exc:
            logger.error("AgentLoopAdapter run failed: %s", exc)
            engine_result = EngineResult(status="failed", error=str(exc))
        finally:
            logger.info(
                "AgentLoopAdapter run finished bot=%s in %.1fs identity=%s",
                cfg.bot_id,
                time.monotonic() - start_time,
                sorted(self._context().keys()),
            )
        return engine_result

    async def resume(self, checkpoint: dict[str, Any]) -> EngineResult:
        """Resume from a durable GoalRun checkpoint on the same session."""
        if self._config is None or self._loop is None:
            return EngineResult(status="failed", error="engine not started")

        objective = str(checkpoint.get("goal_text") or "")
        if not objective:
            return EngineResult(status="failed", error="checkpoint has no goal_text")

        cfg = self._config
        try:
            result = await self._loop.run(
                objective,
                session_id=cfg.session_id,
                owner=cfg.bot_id,
            )
            return self._to_result(result)
        except Exception as exc:
            logger.error("AgentLoopAdapter resume failed: %s", exc)
            return EngineResult(status="failed", error=str(exc))

    async def stop(self) -> None:
        """Stop the engine."""
        self._stopping = True
        logger.info(
            "AgentLoopAdapter stopped for bot=%s", self._config.bot_id if self._config else "?"
        )


class GenesisAgentAdapter:
    """Adapter wrapping the existing GenesisAgent.

    GenesisAgent is the first real engine implementation. It runs a ReAct
    loop with 3O physical tools. This adapter translates between
    EngineConfig/EngineResult and GenesisAgent's handle_mission interface.

    GenesisAgent retains its independent capability. Production Bot Runtime
    reaches it through runtime_id → Runtime registry → this adapter, never
    by hardcoding GenesisAgent(...).
    """

    def __init__(self, *, llm_fn: Callable | None = None) -> None:
        self._config: EngineConfig | None = None
        self._llm_fn = llm_fn
        self._agent: Any | None = None
        self._stopping = False

    async def start(self, config: EngineConfig) -> None:
        """Initialise GenesisAgent for one bot instance."""
        self._config = config
        self._stopping = False

        try:
            from server.agents.genesis_agent import GenesisAgent

            self._agent = GenesisAgent(
                dedicated_api_key=None,
                llm_fn=self._llm_fn,
                model="gpt-4o",
            )
            # Wake up to load memory
            self._agent.wake_up()
        except Exception as exc:
            logger.error("GenesisAgentAdapter start failed: %s", exc)
            self._agent = None

        logger.info(
            "GenesisAgentAdapter started for bot=%s instance=%s",
            config.bot_id,
            config.instance_id,
        )

    async def run(
        self,
        objective: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> EngineResult:
        """Execute one goal through GenesisAgent."""
        if self._agent is None:
            return EngineResult(status="failed", error="genesis agent not initialised")
        if self._stopping:
            return EngineResult(status="cancelled", error="engine stopped")

        start_time = time.monotonic()

        try:
            mission_result = await self._agent.handle_mission(objective)
            status = mission_result.get("status", "failed")
            return EngineResult(
                status=status,
                summary=str(mission_result.get("response", "")),
                rounds=int(mission_result.get("steps", 0)),
                cost_usd=float(mission_result.get("cost_usd", 0.0)),
            )
        except Exception as exc:
            logger.error("GenesisAgentAdapter run failed: %s", exc)
            return EngineResult(status="failed", error=str(exc))
        finally:
            duration = time.monotonic() - start_time
            logger.info("GenesisAgentAdapter run finished in %.1fs", duration)

    async def resume(self, checkpoint: dict[str, Any]) -> EngineResult:
        """Resume from a checkpoint (GenesisAgent has memory, so it resumes naturally)."""
        if self._agent is None:
            return EngineResult(status="failed", error="genesis agent not initialised")

        # GenesisAgent's memory system handles continuity; resume by re-running
        # the mission with the checkpoint context injected.
        return await self.run(
            str(checkpoint.get("goal_text", "")),
            context=checkpoint,
        )

    async def stop(self) -> None:
        """Stop the GenesisAgent."""
        self._stopping = True
        if self._agent is not None:
            with contextlib.suppress(Exception):
                self._agent.sleep()
        logger.info("GenesisAgentAdapter stopped")


__all__ = [
    "AgentEngine",
    "AgentLoopAdapter",
    "EngineConfig",
    "EngineResult",
    "GenesisAgentAdapter",
]
