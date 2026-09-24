"""GoalRun worker adapter for the legacy DaemonEngine trigger surface.

DaemonEngine is intentionally only a trigger/projection layer.  AgentLoop is
kept as the provider implementation for compatibility, while GoalRun owns
the persisted task graph, retry/finalization state, and restart identity.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.goal_run.leaf import LeafResult
from server.goal_run.store import load_goal_run, save_goal_run
from veya.omodul.agent_loop import AgentLoop, LoopResult
from veya.omodul.session_tree import SessionTreeMgr
from veya.omodul.tool_pipeline import ToolPipeline


class DaemonGoalRunAdapter:
    verification_required = True
    skip_plan_review = True
    # Daemon tasks already have a typed AgentLoop result and the canonical
    # GoalRun verification path.  The repository-wide dual-axis code review
    # is advisory and uses the ambient LLM; making it a completion dependency
    # would give a non-authoritative global provider control over daemon
    # terminalization (and breaks deterministic provider-isolation tests).
    skip_advisory_code_review = True

    def __init__(
        self,
        *,
        llm: Any,
        tree: SessionTreeMgr,
        barrier: Any,
        pipeline_factory: Callable[[], ToolPipeline],
        tool_specs: dict[str, tuple[Callable[..., Any], dict | None]],
        system_prompt: str,
        pause_events: dict[str, Any],
        resume_events: dict[str, Any],
        project_root: str,
        projection: Callable[[str, Any], None] | None = None,
        **_legacy_kwargs: Any,
    ) -> None:
        self._llm = llm
        self._tree = tree
        self._barrier = barrier
        self._pipeline_factory = pipeline_factory
        self._tool_specs = tool_specs
        self._system_prompt = system_prompt
        self._pause_events = pause_events
        self._resume_events = resume_events
        self._project_root = project_root
        self._projection = projection

    async def before_execution(self, state: Any, project_root: str) -> None:
        return None

    async def before_iteration(self, state: Any, project_root: str, task: Any) -> None:
        return None

    def checkpoint(self, state: Any, project_root: str, *, reason: str) -> None:
        save_goal_run(state, project_root)

    def _spec(self, task: Any) -> dict[str, Any]:
        return json.loads(task.instruction)

    async def execute_semantic_task(self, state: Any, task: Any) -> LeafResult:
        spec = self._spec(task)
        checkpoint = state.runtime_checkpoint or {}
        daemon_checkpoint = checkpoint.setdefault("daemon", {})
        session_id = str(spec.get("session_id") or daemon_checkpoint.get("session_id") or "")
        if not session_id:
            session_id = self._tree.create_session(system=self._system_prompt or None)
        daemon_checkpoint["session_id"] = session_id
        daemon_checkpoint["task_id"] = spec["task_id"]
        save_goal_run(state, self._project_root)

        pipeline = self._pipeline_factory()
        for name, (fn, schema) in self._tool_specs.items():
            pipeline.register(name, fn, schema=schema)

        pause_event = self._pause_events.setdefault(task.id, _event())
        resume_event = self._resume_events.setdefault(task.id, _event())

        async def gate() -> None:
            while pause_event.is_set():
                resume_event.clear()
                await resume_event.wait()

        loop = AgentLoop(
            llm=self._llm,
            pipeline=pipeline,
            tree=self._tree,
            barrier=self._barrier,
            system_prompt=self._system_prompt,
            gate=gate,
        )
        result = await loop.run(spec["user_input"], session_id=session_id)
        daemon_checkpoint["result"] = result.to_dict()
        daemon_checkpoint["session_id"] = result.session_id
        save_goal_run(state, self._project_root)
        if self._projection is not None:
            self._projection(spec["task_id"], result)
        if result.error:
            return LeafResult(
                status="blocked",
                summary=result.final_answer,
                block_reason=result.error,
                stop_reason=result.stop_reason or "provider_error",
            )
        return LeafResult(
            status="completed",
            summary=result.final_answer,
            stop_reason=result.stop_reason or "completed",
        )


def daemon_task(task_id: str, user_input: str, session_id: str) -> dict[str, Any]:
    return {
        "id": f"daemon:{task_id}",
        "title": f"Daemon task {task_id}",
        "instruction": json.dumps(
            {"task_id": task_id, "user_input": user_input, "session_id": session_id},
            ensure_ascii=False,
            sort_keys=True,
        ),
        "acceptance": ["AgentLoop provider returned a terminal result"],
        "assignee": "builtin",
    }


def unfinished_daemon_runs(project_root: str) -> list[tuple[str, Any, str, str, str]]:
    root = Path(project_root) / ".veya-project" / "goal-runs"
    if not root.is_dir():
        return []
    terminal = {"completed", "partial_completed", "failed", "cancelled", "blocked"}
    found: list[tuple[str, Any, str, str, str]] = []
    for graph in sorted(root.glob("*/taskgraph.json")):
        goal_id = graph.parent.name
        state = load_goal_run(project_root, goal_id)
        if state is None or state.status.value in terminal:
            continue
        task = next((item for item in state.tasks.values() if item.id.startswith("daemon:")), None)
        if task is not None:
            spec = json.loads(task.instruction)
            found.append(
                (
                    goal_id,
                    state,
                    str(spec["task_id"]),
                    str(spec["user_input"]),
                    str(spec.get("session_id") or ""),
                )
            )
    return found


class DaemonGoalRunBridge:
    """Application-owned durable bridge injected into the 3O DaemonEngine."""

    @staticmethod
    def _adapter(
        engine: Any,
        tool_specs: dict[str, tuple[Callable[..., Any], dict | None]],
    ) -> DaemonGoalRunAdapter:
        return DaemonGoalRunAdapter(
            llm=engine._llm,
            tree=engine._tree,
            barrier=engine._barrier,
            pipeline_factory=engine._pipeline_factory,
            tool_specs=tool_specs,
            system_prompt=engine._system_prompt,
            pause_events=engine._pause_events,
            resume_events=engine._resume_events,
            project_root=engine._project_root,
            projection=engine._project_result,
        )

    async def run_goal(
        self,
        engine: Any,
        task_id: str,
        state: Any,
        tools: dict[str, tuple[Callable[..., Any], dict | None]],
    ) -> Any:
        from server.goal_run.runner import project_run_goal

        return await project_run_goal(
            project_root=engine._project_root,
            goal=f"Daemon task {task_id}",
            tasks=[daemon_task(task_id, state.user_input, state.session_id)],
            mode="act_eager",
            integration_adapter=self._adapter(engine, tools),
            max_wall_s=7200,
        )

    async def resume_goal(self, engine: Any, goal_id: str, task_id: str, state: Any) -> Any:
        from server.goal_run.runner import project_run_goal

        return await project_run_goal(
            project_root=engine._project_root,
            goal=state.user_input,
            mode="act_eager",
            resume_goal_id=goal_id,
            integration_adapter=self._adapter(engine, engine._tool_specs),
        )

    async def recover_running_tasks(self, engine: Any) -> int:
        import asyncio

        from veya.oservi.daemon_engine import TaskState, TaskStatus

        recovered = 0
        for (
            goal_id,
            durable_state,
            task_id,
            user_input,
            stored_session_id,
        ) in unfinished_daemon_runs(engine._project_root):
            checkpoint = durable_state.runtime_checkpoint or {}
            daemon_checkpoint = checkpoint.get("daemon") or {}
            session_id = str(daemon_checkpoint.get("session_id") or stored_session_id or "")
            result_data = daemon_checkpoint.get("result") or {}
            result = (
                LoopResult(
                    **{
                        key: result_data[key]
                        for key in (
                            "session_id",
                            "final_answer",
                            "rounds",
                            "stop_kind",
                            "stop_reason",
                            "tool_calls",
                            "tool_failures",
                            "error",
                            "cost_usd",
                        )
                        if key in result_data
                    }
                )
                if result_data
                else None
            )
            state = TaskState(
                task_id=task_id,
                user_input=user_input,
                goal_id=goal_id,
                status=TaskStatus.PAUSED if daemon_checkpoint.get("paused") else TaskStatus.PENDING,
                session_id=session_id,
                result=result,
            )
            engine._tasks[task_id] = state
            if state.status == TaskStatus.PAUSED:
                continue
            engine._drivers[task_id] = asyncio.create_task(
                engine._resume_goal(goal_id, task_id, state),
                name=f"veya-daemon-recovery-{task_id}",
            )
            recovered += 1
        return recovered

    def goal_id_for_task(self, engine: Any, task_id: str) -> str | None:
        return next(
            (
                goal_id
                for goal_id, _, found, _, _ in unfinished_daemon_runs(engine._project_root)
                if found == task_id
            ),
            None,
        )

    def persist_pause(self, engine: Any, task_id: str, paused: bool) -> None:
        goal_id = self.goal_id_for_task(engine, task_id)
        if not goal_id:
            return
        durable_state = load_goal_run(engine._project_root, goal_id)
        if durable_state is None:
            return
        checkpoint = durable_state.runtime_checkpoint or {}
        checkpoint.setdefault("daemon", {})["paused"] = paused
        durable_state.runtime_checkpoint = checkpoint
        save_goal_run(durable_state, engine._project_root)


def _event():
    import asyncio

    return asyncio.Event()


__all__ = ["DaemonGoalRunAdapter", "DaemonGoalRunBridge", "daemon_task", "unfinished_daemon_runs"]
