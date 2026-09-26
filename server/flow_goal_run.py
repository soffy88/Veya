"""Durable adapter for the Genesis HITL workflow phase.

The workflow plane keeps its typed Genesis semantics, while GoalRun owns
durability, task state, checkpointing, and finalization.  This adapter has no
scheduler and does not create a nested GoalRun.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from server.goal_run.leaf import LeafResult
from server.goal_run.models import GoalStatus
from server.goal_run.runner import project_run_goal
from server.goal_run.store import load_goal_run, save_goal_run
from server.schemas import GenesisManifest
from server.sse import emit
from veya.llm import llm_call


class GenesisGoalRunAdapter:
    """Execute one typed Genesis phase as a GoalRun semantic leaf."""

    verification_required = True
    # A1-F: This logic will move to PreplannedExecutionSpec constraints
    skip_plan_review = True
    skip_advisory_code_review = True

    def __init__(
        self,
        manifest: GenesisManifest | None,
        *,
        config: dict[str, Any] | None = None,
        project_root: str = ".",
    ):
        self.manifest = manifest
        self.config = dict(config or {})
        self.project_root = project_root

    async def before_execution(self, state: Any, project_root: str) -> None:
        return None

    async def before_iteration(self, state: Any, project_root: str, task: Any) -> None:
        return None

    def checkpoint(self, state: Any, project_root: str, *, reason: str) -> None:
        save_goal_run(state, project_root)

    def _manifest_for(self, task: Any) -> GenesisManifest:
        if self.manifest is not None:
            return self.manifest
        payload = json.loads(task.instruction)
        self.manifest = GenesisManifest.model_validate(payload)
        return self.manifest

    @staticmethod
    def _checkpoint(state: Any) -> dict[str, Any]:
        root = state.runtime_checkpoint
        if not isinstance(root, dict):
            root = {}
            state.runtime_checkpoint = root
        phase = root.setdefault(
            "genesis_phase3",
            {"completed_elements": [], "results": [], "assembly_done": False},
        )
        phase.setdefault("completed_elements", [])
        phase.setdefault("results", [])
        phase.setdefault("assembly_done", False)
        return phase

    async def execute_semantic_task(self, state: Any, task: Any) -> LeafResult:
        manifest = self._manifest_for(task)
        phase = self._checkpoint(state)
        completed = set(phase["completed_elements"])
        results = list(phase["results"])
        root = Path(self.project_root) / ".veya-project" / "goal-runs" / state.goal_id / "genesis"
        root.mkdir(parents=True, exist_ok=True)

        from server.agents.genesis_agent import GenesisAgent

        for element in manifest.elements:
            key = f"{element.layer}:{element.name}"
            if key in completed:
                continue
            emit(
                state.goal_id,
                "genesis_element_start",
                {"layer": element.layer, "name": element.name, "goal_id": state.goal_id},
            )
            try:
                agent = GenesisAgent(dedicated_api_key=os.environ.get("GENESIS_API_KEY"))
                agent.wake_up()
                try:
                    mission = (
                        f"Check if {element.name} exists in {element.layer}. If not, implement it "
                        f"matching these specs: {element.specs}"
                    )
                    result = await agent.handle_mission(mission)
                finally:
                    agent.sleep()
                entry = {"layer": element.layer, "name": element.name, **result}
            except Exception as exc:
                entry = {
                    "layer": element.layer,
                    "name": element.name,
                    "status": "failed",
                    "error": str(exc),
                }
            results = [
                previous
                for previous in results
                if f"{previous.get('layer')}:{previous.get('name')}" != key
            ]
            results.append(entry)
            phase["results"] = results
            if entry.get("status") != "failed" and key not in phase["completed_elements"]:
                phase["completed_elements"].append(key)
            save_goal_run(state, self.project_root)
            emit(
                state.goal_id,
                "genesis_element_done",
                {**entry, "goal_id": state.goal_id},
            )

        failures = [item for item in results if item.get("status") == "failed"]
        if failures:
            return LeafResult(
                status="blocked",
                summary="Genesis element forging failed",
                block_reason=json.dumps(failures, ensure_ascii=False),
                artifacts=[],
                stop_reason="exception",
                unfinished_work=[item.get("name", "") for item in failures],
            )

        if not phase["assembly_done"]:
            assembly_prompt = (
                "Genesis has completed the following 3O elements:\n"
                f"{json.dumps(results, ensure_ascii=False, indent=2)}\n\n"
                "Write the final integration script (Python) that glues these elements together "
                "into a single working entry point. Return only the code, no commentary."
            )
            try:
                response = await llm_call(
                    [{"role": "user", "content": assembly_prompt}],
                    config=self.config,
                    max_tokens=4096,
                )
                code = ((response.get("choices") or [{}])[0].get("message") or {}).get(
                    "content"
                ) or ""
                if not code.strip():
                    raise ValueError("assembly returned empty code")
                artifact = root / "assembly.py"
                artifact.write_text(code, encoding="utf-8")
                phase["assembly_done"] = True
                phase["assembly_artifact"] = str(artifact)
                save_goal_run(state, self.project_root)
                emit(
                    state.goal_id,
                    "assembly_done",
                    {"code": code, "mission_id": manifest.mission_id, "goal_id": state.goal_id},
                )
            except Exception as exc:
                emit(
                    state.goal_id,
                    "flow_error",
                    {"stage": "assembly", "error": str(exc), "goal_id": state.goal_id},
                )
                return LeafResult(
                    status="blocked",
                    summary="Genesis assembly failed",
                    block_reason=str(exc),
                    artifacts=[],
                    stop_reason="exception",
                )

        return LeafResult(
            status="completed",
            summary=f"Genesis workflow completed for {manifest.mission_id}",
            artifacts=[phase["assembly_artifact"]],
            stop_reason="completed",
        )


_RECOVERY_TASKS: set[Any] = set()


async def recover_genesis_goal_runs(project_root: str = ".") -> int:
    """Resume unfinished Genesis GoalRuns after a host restart."""

    root = Path(project_root).expanduser().resolve()
    runs_root = root / ".veya-project" / "goal-runs"
    if not runs_root.is_dir():
        return 0

    recovered = 0
    for taskgraph in sorted(runs_root.glob("*/taskgraph.json")):
        goal_id = taskgraph.parent.name
        state = load_goal_run(str(root), goal_id)
        if state is None or state.status in {
            GoalStatus.completed,
            GoalStatus.partial_completed,
            GoalStatus.failed,
            GoalStatus.cancelled,
            GoalStatus.blocked,
        }:
            continue
        genesis_task = next(
            (task for task in state.tasks.values() if task.id.startswith("genesis_phase3:")),
            None,
        )
        if genesis_task is None:
            continue
        try:
            manifest = GenesisManifest.model_validate(json.loads(genesis_task.instruction))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue

        async def _resume(
            *,
            _goal_id: str = goal_id,
            _manifest: GenesisManifest = manifest,
        ) -> None:
            await project_run_goal(
                project_root=str(root),
                goal=f"Genesis workflow {_manifest.mission_id}",
                mode="act_eager",
                resume_goal_id=_goal_id,
                integration_adapter=GenesisGoalRunAdapter(_manifest, project_root=str(root)),
            )

        task = asyncio.create_task(_resume(), name=f"veya-genesis-recovery-{goal_id}")
        _RECOVERY_TASKS.add(task)
        task.add_done_callback(_RECOVERY_TASKS.discard)
        recovered += 1
    return recovered


def phase3_task(manifest: GenesisManifest) -> dict[str, Any]:
    """Build the single opaque GoalRun leaf without semantic decomposition."""

    return {
        "id": f"genesis_phase3:{manifest.mission_id}",
        "title": f"Genesis phase3: {manifest.mission_id}",
        "instruction": json.dumps(manifest.model_dump(), ensure_ascii=False, sort_keys=True),
        "acceptance": ["Genesis elements forged and assembly.py written"],
        "assignee": "builtin",
    }


__all__ = ["GenesisGoalRunAdapter", "phase3_task", "recover_genesis_goal_runs"]
