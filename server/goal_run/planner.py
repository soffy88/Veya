"""goal_run planner — explicit task graph compilation for GoalRun.

实现要点：
- GoalRun does not reinterpret natural-language user intent. The MasterAgent
  supplies the objective (and may supply an explicit task graph).
- G1: compile explicit tasks or a single opaque objective into a durable task
  graph; execute/verify remains the only responsibility of this module.
- 依赖现有 understand() 与 project_ask 契约；不重复造轮子。
"""

from datetime import UTC, datetime
from typing import Any

from server.goal_run.models import GoalRunResponse, GoalRunState, GoalStatus, TaskNode
from server.project_understand import UnderstandResult

# ── G0 — Understand 入口 ────────────────────────────────────────────────


async def g0_understand(
    goal_text: str,
    memory: str,
    chain: list[dict[str, Any]] | None = None,
    mode: str = "auto",
) -> tuple[UnderstandResult, GoalRunResponse | None]:
    """G0 门禁：调用现有 understand() 完成判定。

    返回 (understand_result, response_or_none)。
    response 为 None 表示还没要返回 response，仅返回 result。
    当 decision=ask 时，response 包含 understood_ask 相关信息。
    """

    from server.project_ask import understand  # 复用既有门禁

    u = await understand(goal_text, memory, chain)

    if mode == "act_eager":
        # 跳过判定，直接认定为 act
        u = UnderstandResult(
            decision="act",
            confidence=1.0,
            interpretation=goal_text[:500],
            assumptions=["act_eager: 未做澄清"],
            questions=[],
            risk_flags=[],
            reasons=["mode=act_eager"],
        )

    # 构造响应（视决策而定）
    response = None
    if u.decision == "ask":
        response = GoalRunResponse(
            goal_id="temp_" + goal_text[:20],
            status=GoalStatus.awaiting_user,
            phase="understood_ask",
            interpretation=u.interpretation,
            questions=u.questions,
            goal_counts=None,
            summary=None,
            block_reason=None,
            artifacts=None,
            next_action="wait",
        )
    elif u.decision == "act":
        response = GoalRunResponse(
            goal_id="temp_" + goal_text[:20],
            status=GoalStatus.running,
            phase="planning",  # 进入 G1 Plan
            interpretation=u.interpretation,
            questions=None,
            goal_counts=None,
            summary=None,
            block_reason=None,
            artifacts=None,
            next_action="plan",
        )

    return u, response


# ── G1 — Plan 任务图生成 ─────────────────────────────────────────────────


async def g1_plan(
    interpretation: str,
    assumptions: list[str],
    goal_text: str,
    default_assignee: str = "hicode",
    budget: dict[str, int] | None = None,
    max_leaf_tasks: int | None = None,
    project_root: str | None = None,
    explicit_tasks: list[dict[str, Any]] | None = None,
) -> tuple[GoalRunState, GoalRunResponse]:
    """G1 Plan：根据 interpretation 生成任务图 taskgraph.json。

    实现要点：
    - LLM 调用（或规则生成）产生任务列表
    - 每任务必须有非空 acceptance
    - depends_on 无环；失败则 blocked
    - 默认所有 assignee = default_assignee（忽略模型想写的 Sol/Luna）
    - 落盘 GOAL.md + taskgraph.json
    """

    import json
    from pathlib import Path

    if budget is None:
        budget = {"max_wall_s": 7200, "max_leaf_tasks": 40, "max_retries_per_task": 2}

    if max_leaf_tasks is None:
        max_leaf_tasks = budget.get("max_leaf_tasks", 40)

    if project_root:
        spec_state = await _g1_from_speckit(
            project_root=project_root,
            goal_text=goal_text,
            interpretation=interpretation,
            assumptions=assumptions,
            default_assignee=default_assignee,
            budget=budget,
            max_leaf_tasks=max_leaf_tasks,
        )
        if spec_state is not None:
            return spec_state

    # Task decomposition is an input contract from the MasterAgent.  If the
    # caller has not supplied a graph, preserve the objective as one opaque
    # leaf instead of guessing sub-goals with keywords.
    tasks = _normalize_explicit_tasks(explicit_tasks, default_assignee, max_leaf_tasks)
    if not tasks:
        tasks = _generate_tasks_rules(interpretation, assumptions, default_assignee, max_leaf_tasks)

    # ── 构造 GoalRunState ──
    goal_id = f"goal_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    state = GoalRunState(
        goal_id=goal_id,
        goal_text=goal_text,
        status=GoalStatus.running,
        default_assignee=default_assignee,
        budget=budget,
    )

    for tn in tasks:
        state.tasks[tn["id"]] = TaskNode(
            id=tn["id"],
            title=tn["title"],
            instruction=tn["instruction"],
            acceptance=tn["acceptance"],
            depends_on=tn.get("depends_on", []),
            assignee=tn.get("assignee", default_assignee),
            parallel=bool(tn.get("parallel", False)),
        )

    # ── 落盘 ──
    # 写入 taskgraph.json
    root = Path(project_root or ".")
    taskgraph_path = root / ".veya-project" / "goal-runs" / goal_id / "taskgraph.json"
    taskgraph_path.parent.mkdir(parents=True, exist_ok=True)
    taskgraph_path.write_text(
        json.dumps(state.to_taskgraph_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 写入 GOAL.md
    goal_md_path = root / ".veya-project" / "goal-runs" / goal_id / "GOAL.md"
    goal_md_path.parent.mkdir(parents=True, exist_ok=True)
    goal_md_path.write_text(
        f"# Goal: {goal_text}\n\n"
        f"## Interpretation\n{interpretation}\n\n"
        f"## Assumptions\n"
        + "\n".join(f"- {a}" for a in assumptions)
        + f"\n\n## Task Graph\nGenerated at {datetime.now(UTC).isoformat()}",
        encoding="utf-8",
    )

    # 事件记录
    events_path = root / ".veya-project" / "goal-runs" / goal_id / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps(
            {
                "type": "plan_created",
                "goal_id": goal_id,
                "goal_text": goal_text,
                "task_count": len(tasks),
                "generated_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    # 构造响应
    response = GoalRunResponse(
        goal_id=goal_id,
        status=GoalStatus.running,
        phase="running",
        interpretation=interpretation,
        questions=None,
        goal_counts=state.snapshot_running(),
        summary=None,
        block_reason=None,
        artifacts=[str(taskgraph_path), str(goal_md_path)],
        next_action="run_loop",
    )

    return state, response


async def _g1_from_speckit(
    *,
    project_root: str,
    goal_text: str,
    interpretation: str,
    assumptions: list[str],
    default_assignee: str,
    budget: dict[str, int],
    max_leaf_tasks: int,
) -> tuple[GoalRunState, GoalRunResponse] | None:
    """When .speckit/{tasks,constitution}.md exist, compile that SSOT."""
    from pathlib import Path

    from omodul.phase_spec_driven_plan import phase_spec_driven_plan

    root = Path(project_root)
    if not (root / ".speckit" / "tasks.md").is_file():
        return None
    if not (root / ".speckit" / "constitution.md").is_file():
        return None
    goal_id = f"goal_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    rec = await phase_spec_driven_plan(
        {
            "goal_id": goal_id,
            "project_root": str(root),
            "max_leaf_tasks": max_leaf_tasks,
        },
        {},
        root / ".veya-project" / "goal-runs" / goal_id,
    )
    if rec.get("status") != "completed":
        return None
    findings = rec.get("findings") or {}
    state = GoalRunState(
        goal_id=goal_id,
        goal_text=goal_text,
        constitution=str(findings.get("constitution") or ""),
        status=GoalStatus.running,
        default_assignee=default_assignee,
        budget=budget,
    )
    # [P] 并行标记(smart-ralph 内化, 见 memory project_veya_pi_gap_audit): 单独
    # 重读一遍原始 tasks.md 提取标记——不碰 oskill.dag_compiler(3O 主库), 结果
    # 按任务 id 跟它已经解析出的 findings["tasks"] 对齐, 见
    # server/goal_run/parallel_markers.py 文件头。
    from server.goal_run.parallel_markers import extract_parallel_task_ids

    try:
        tasks_md_text = (root / ".speckit" / "tasks.md").read_text(encoding="utf-8")
        parallel_ids = extract_parallel_task_ids(tasks_md_text)
    except OSError:
        parallel_ids = set()
    for td in findings.get("tasks") or []:
        state.tasks[td["id"]] = TaskNode(
            id=td["id"],
            title=td["title"],
            instruction=td.get("instruction") or td["title"],
            acceptance=td.get("acceptance") or [td["title"]],
            depends_on=td.get("depends_on") or [],
            assignee=td.get("assignee") or default_assignee,
            parallel=td["id"] in parallel_ids,
        )
    from server.goal_run.store import save_goal_run

    save_goal_run(state, project_root)
    response = GoalRunResponse(
        goal_id=goal_id,
        status=GoalStatus.running,
        phase="running",
        interpretation=interpretation,
        questions=None,
        goal_counts=state.snapshot_running(),
        summary=None,
        block_reason=None,
        artifacts=[str(findings.get("taskgraph_path") or "")],
        next_action="run_loop",
    )
    return state, response


def _generate_tasks_rules(
    interpretation: str,
    assumptions: list[str],
    default_assignee: str,
    max_leaf_tasks: int,
) -> list[dict[str, Any]]:
    """Compile an opaque objective as one leaf when no graph was supplied."""
    del assumptions, max_leaf_tasks
    instruction = (interpretation or "").strip()
    if not instruction:
        return []
    return [
        {
            "id": "t1",
            "title": instruction[:80],
            "instruction": instruction,
            "acceptance": ["目标执行产生可观察结果"],
            "depends_on": [],
            "assignee": default_assignee,
        }
    ]


def _normalize_explicit_tasks(
    tasks: list[dict[str, Any]] | None,
    default_assignee: str,
    max_leaf_tasks: int,
) -> list[dict[str, Any]]:
    """Validate the model-supplied task graph without semantic expansion."""
    if not isinstance(tasks, list):
        return []
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(tasks[:max_leaf_tasks], start=1):
        if not isinstance(raw, dict):
            continue
        instruction = str(raw.get("instruction") or raw.get("title") or "").strip()
        if not instruction:
            continue
        task_id = str(raw.get("id") or f"t{index}")
        normalized.append(
            {
                "id": task_id,
                "title": str(raw.get("title") or instruction[:80]),
                "instruction": instruction,
                "acceptance": [str(item) for item in (raw.get("acceptance") or []) if str(item).strip()]
                or ["目标执行产生可观察结果"],
                "depends_on": [str(item) for item in (raw.get("depends_on") or [])],
                "assignee": str(raw.get("assignee") or default_assignee),
                "parallel": bool(raw.get("parallel", False)),
            }
        )
    return normalized


# ── 辅助：将旧格式 taskgraph 迁移到新模型 ────────────────────────────────


async def migrate_old_taskgraph(
    project_root: str,
    goal_id: str,
) -> GoalRunState | None:
    """从旧的 project_ask 任务图迁移到新的 goal_run 状态（v0.1 兼容）。

    实现要点：读取旧的 runs/<task_id>/taskgraph.json（若存在）并转换为
    GoalRunState，不丢失已完成任务与产物。
    """
    import json

    from server.goal_run.store import save_goal_run
    from server.project_store import ProjectStore

    store = ProjectStore(project_root)
    # 尝试读取旧格式
    runs_dir = store.dir / "runs"
    if not runs_dir.exists():
        return None

    # 查找匹配 goal_id 的 run
    for run_dir in runs_dir.iterdir():
        tg_path = run_dir / "taskgraph.json"
        if not tg_path.exists():
            continue
        try:
            with open(tg_path, encoding="utf-8") as f:
                old_data = json.load(f)
            # 迁移：将旧字段映射到新模型
            state = GoalRunState.from_taskgraph_json(old_data, "")
            state.goal_id = goal_id
            # 保存新格式 (save_goal_run 是同步函数, 跟其余全部调用点一致, 不 await;
            # 第二个参数是 project_root 不是 goal_id, 之前传错了)
            save_goal_run(state, project_root)
            return state
        except Exception:
            continue

    return None
