"""Layer 4 glue: assemble BossOrchestrationEngine. Does not rename project_run_goal."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from obase.workspace_snapshot import WorkspaceInspector
from omodul.phase_closed_loop_plan import phase_closed_loop_plan
from omodul.phase_evidence_verify import phase_evidence_verify
from omodul.phase_intent_triage import phase_intent_triage
from oprim._jailed_executor import execute_leaf_with_constitution
from oservi.engines.boss_orchestration import BossOrchestrationEngine


async def project_run_goal_boss_mode(
    project_root: str,
    goal: str,
    *,
    goal_id: str = "boss",
    llm_caller: Callable[..., Any] | None = None,
    leaf_executor: Callable[..., Any] | None = None,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Start the contractor engine. Leaves stay on hicode/dsh; Veya does not code."""
    root = Path(project_root)
    inspector = WorkspaceInspector(root)
    out = Path(output_dir) if output_dir else root / ".veya-project" / "goal-runs" / goal_id
    engine = BossOrchestrationEngine(
        inspector=inspector.capture_snapshot,
        intent_phase=phase_intent_triage,
        plan_phase=phase_closed_loop_plan,
        verify_phase=phase_evidence_verify,
        leaf_executor=leaf_executor or _default_leaf,
        llm_caller=llm_caller or _veya_llm_caller,
        output_dir=out,
        name="veya_boss_mode",
    )
    return await engine.run_goal(root, goal, goal_id=goal_id)


async def _default_leaf(
    project_root: Path | str,
    *,
    instruction: str,
    assignee: str = "hicode",
    constitution_text: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    rec = await execute_leaf_with_constitution(
        project_root,
        instruction=instruction,
        constitution_text=constitution_text,
        assignee=assignee,
        runner=_veya_leaf_runner,
    )
    inspector = WorkspaceInspector(project_root)
    snap = await inspector.capture_snapshot()
    rec.setdefault("stdout", rec.get("brief") or rec.get("summary") or "")
    rec.setdefault("git_diff", snap.git_diff)
    return rec


async def _veya_leaf_runner(
    *,
    project_root: str,
    instruction: str,
    assignee: str = "hicode",
    **kwargs: Any,
) -> dict[str, Any]:
    from server.goal_run.leaf import execute_leaf_with_memory

    leaf = await execute_leaf_with_memory(
        project_root=project_root,
        instruction=instruction,
        acceptance=[],
        assignee=assignee,
    )
    return {
        "status": getattr(leaf, "status", "blocked"),
        "stdout": getattr(leaf, "summary", "") or "",
        "summary": getattr(leaf, "summary", "") or "",
        "artifacts": list(getattr(leaf, "artifacts", None) or []),
    }


async def _veya_llm_caller(
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 4096,
    **kwargs: Any,
) -> dict[str, Any]:
    from server.project_understand import _default_llm

    system_parts: list[str] = []
    user_parts: list[str] = []
    for msg in messages:
        role = msg.get("role")
        content = str(msg.get("content") or "")
        if role == "system":
            system_parts.append(content)
        else:
            user_parts.append(content)
    text = await _default_llm("\n".join(system_parts), "\n".join(user_parts))
    return {"ok": True, "content": text}
