"""GoalRun adapter for the Automata grid-search workflow."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from server.goal_run.leaf import LeafResult
from server.goal_run.models import GoalStatus
from server.goal_run.runner import project_run_goal
from server.goal_run.store import load_goal_run, save_goal_run

_recovery_tasks: set[asyncio.Task[Any]] = set()


class GridSearchGoalRunAdapter:
    verification_required = True
    skip_plan_review = True
    skip_advisory_code_review = True

    def __init__(
        self,
        *,
        asset_id: str | None = None,
        strategy_code: str | None = None,
        param_grid: dict[str, Any] | None = None,
        session_id: str | None = None,
        project_root: str = ".",
    ):
        self.asset_id = asset_id
        self.strategy_code = strategy_code
        self.param_grid = dict(param_grid or {})
        self.session_id = session_id
        self.project_root = project_root

    async def before_execution(self, state: Any, project_root: str) -> None:
        return None

    async def before_iteration(self, state: Any, project_root: str, task: Any) -> None:
        return None

    def checkpoint(self, state: Any, project_root: str, *, reason: str) -> None:
        save_goal_run(state, project_root)

    def _spec(self, task: Any) -> dict[str, Any]:
        if self.asset_id is None or self.strategy_code is None:
            spec = json.loads(task.instruction)
            self.asset_id = str(spec["asset_id"])
            self.strategy_code = str(spec["strategy_code"])
            self.param_grid = dict(spec["param_grid"])
            self.session_id = spec.get("session_id")
        return {
            "asset_id": self.asset_id,
            "strategy_code": self.strategy_code,
            "param_grid": self.param_grid,
            "session_id": self.session_id,
        }

    async def execute_semantic_task(self, state: Any, task: Any) -> LeafResult:
        spec = self._spec(task)
        checkpoint = state.runtime_checkpoint
        if not isinstance(checkpoint, dict):
            checkpoint = {}
            state.runtime_checkpoint = checkpoint
        grid = checkpoint.setdefault("grid_search", {})
        root = Path(self.project_root) / ".veya-project" / "goal-runs" / state.goal_id
        root.mkdir(parents=True, exist_ok=True)

        if "results" not in grid:
            from server.notification_center import global_notifier
            from server.quant_coprocessor import quant_coprocessor

            def on_progress(done: int, total: int, latest: dict[str, Any]) -> None:
                detail = (
                    f"最新 Sharpe: {latest['sharpe']:.2f}"
                    if "sharpe" in latest
                    else f"失败: {str(latest.get('error', ''))[:100]}"
                )
                global_notifier.push(
                    "INFO",
                    f"网格搜索进行中 ({done}/{total})",
                    detail,
                    {
                        "task_id": task.id,
                        "session_id": spec["session_id"],
                        "done": done,
                        "total": total,
                    },
                )

            results = await quant_coprocessor.execute_grid_search(
                spec["strategy_code"],
                spec["asset_id"],
                spec["param_grid"],
                progress_callback=on_progress,
            )
            from veya.platform import oprim as _load_oprim

            best = _load_oprim().reduce_best(results)
            if best is None:
                return LeafResult(
                    status="blocked",
                    summary="所有参数组合均报错",
                    block_reason="grid search produced no valid result",
                    stop_reason="exception",
                )
            grid["results"] = results
            grid["best"] = best
            save_goal_run(state, self.project_root)

        best = grid["best"]
        results = grid["results"]
        if "summary" not in grid:
            from server.notification_center import global_notifier

            prompt = (
                "[SYSTEM TRIGGER] The backend grid search for "
                f"{spec['asset_id']} has completed. Task: {task.id}.\n"
                f"Search space: {spec['param_grid']}\n"
                f"Total combinations tested: {len(results)}\n"
                f"Best Result: {json.dumps(best, ensure_ascii=False)}\n"
                f"All results: {json.dumps(results, ensure_ascii=False)}\n\n"
                "Generate a concise summary and a veya-artifact visualization."
            )
            from server.automata import get_automata

            try:
                summary = await get_automata()._scheduler.execute_callback(prompt)
            except Exception as exc:
                summary = f"(无头合成失败: {exc})"
            grid["summary"] = str(summary)
            from runtime.execution.artifacts import write_json_file

            artifact_path = write_json_file(
                root / "grid_search.json",
                {"task_id": task.id, "best": best, "results": results, "summary": summary},
            )
            grid["artifact"] = str(artifact_path)
            save_goal_run(state, self.project_root)
            global_notifier.push(
                "SUCCESS",
                "🎯 网格搜索完成",
                f"最优参数 {best['params']}，Sharpe: {best['sharpe']:.2f}",
                {
                    "task_id": task.id,
                    "session_id": spec["session_id"],
                    "best": best,
                    "content": summary,
                },
            )

        return LeafResult(
            status="completed",
            summary=grid["summary"],
            artifacts=[grid["artifact"]],
            stop_reason="completed",
        )


def grid_search_task(
    task_id: str,
    asset_id: str,
    strategy_code: str,
    param_grid: dict[str, Any],
    session_id: str | None,
) -> dict[str, Any]:
    return {
        "id": f"grid_search:{task_id}",
        "title": f"Grid search: {asset_id}",
        "instruction": json.dumps(
            {
                "asset_id": asset_id,
                "strategy_code": strategy_code,
                "param_grid": param_grid,
                "session_id": session_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "acceptance": ["grid search result and summary artifact written"],
        "assignee": "builtin",
    }


async def recover_grid_search_goal_runs(project_root: str = ".") -> int:
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
        task = next(
            (item for item in state.tasks.values() if item.id.startswith("grid_search:")), None
        )
        if task is None:
            continue

        async def _resume(_goal_id: str = goal_id, _goal_text: str = state.goal_text) -> None:
            await project_run_goal(
                project_root=str(root),
                goal=_goal_text,
                mode="act_eager",
                resume_goal_id=_goal_id,
                integration_adapter=GridSearchGoalRunAdapter(project_root=str(root)),
            )

        task_handle = asyncio.create_task(_resume(), name=f"veya-grid-recovery-{goal_id}")
        _recovery_tasks.add(task_handle)
        task_handle.add_done_callback(_recovery_tasks.discard)
        recovered += 1
    return recovered


__all__ = ["GridSearchGoalRunAdapter", "grid_search_task", "recover_grid_search_goal_runs"]
