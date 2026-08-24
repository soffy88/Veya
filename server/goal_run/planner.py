"""goal_run planner — G0 Understand / G1 Plan 阶段。

实现要点：
- G0: 调用现有 understand() 完成门禁判定；decision=ask → awaiting_user；
  decision=act → 进入 G1；mode=act_eager 直接放行。
- G1: LLM 计划任务图 + 每任务 acceptance；落盘 GOAL.md + taskgraph.json。
- 依赖现有 understand() 与 project_ask 契约；不重复造轮子。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from server.project_understand import UnderstandResult
from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus, GoalRunResponse


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

    # ── LLM 计划调用 ──
    # 这里简化处理：使用现有 understand 逻辑的增强版提示，
    # 实际上应调用 LLM 生成 DAG。此处使用占位规则生成任务。
    tasks = _generate_tasks_rules(interpretation, assumptions, default_assignee, max_leaf_tasks)

    # ── 构造 GoalRunState ──
    goal_id = f"goal_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
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
        )

    # ── 落盘 ──
    from server.goal_run.store import save_goal_run, load_goal_run

    # 写入 taskgraph.json
    taskgraph_path = Path(f".veya-project/goal-runs/{goal_id}/taskgraph.json")
    taskgraph_path.parent.mkdir(parents=True, exist_ok=True)
    taskgraph_path.write_text(
        json.dumps(state.to_taskgraph_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 写入 GOAL.md
    goal_md_path = Path(f".veya-project/goal-runs/{goal_id}/GOAL.md")
    goal_md_path.parent.mkdir(parents=True, exist_ok=True)
    goal_md_path.write_text(
        f"# Goal: {goal_text}\n\n"
        f"## Interpretation\n{interpretation}\n\n"
        f"## Assumptions\n"
        + "\n".join(f"- {a}" for a in assumptions)
        + f"\n\n## Task Graph\nGenerated at {datetime.now(timezone.utc).isoformat()}",
        encoding="utf-8",
    )

    # 事件记录
    events_path = Path(f".veya-project/goal-runs/{goal_id}/events.jsonl")
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps(
            {
                "type": "plan_created",
                "goal_id": goal_id,
                "goal_text": goal_text,
                "task_count": len(tasks),
                "generated_at": datetime.now(timezone.utc).isoformat(),
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
    goal_id = f"goal_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
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
    """规则生成任务（占位，实际应 LLM 调用）。

    实现要点：
    - 从 interpretation 中提取子目标
    - 每任务自动生成 acceptance（观察条件）
    - 依赖关系按语义顺序排列
    - 返回列表中的任务数 ≤ max_leaf_tasks
    """
    # 占位实现：解析 interpretation 中的关键动词，生成基础任务
    # 实际生产中此处应调用 LLM (e.g. via opencode-go) 生成结构化 DAG
    tasks = []
    # 简单的关键词提取作为演示
    key_verbs = ["实现", "修复", "创建", "分析", "测试", "部署"]

    # 解析 interpretation 获取关键指令
    instr = interpretation[:200] if interpretation else ""

    # 生成最多 max_leaf_tasks 个任务
    for i in range(min(max_leaf_tasks, 5)):  # 演示最多 5 个任务
        task_id = f"t{i + 1}"
        title = f"任务 {i + 1}: {instr[:30]}..."
        instruction = f"{instr}（子任务 {i + 1}）"
        acceptance = [f"可观察条件 {i + 1}: {task_id} 完成"]
        depends_on = [] if i == 0 else [f"t{i}"]

        tasks.append(
            {
                "id": task_id,
                "title": title,
                "instruction": instruction,
                "acceptance": acceptance,
                "depends_on": depends_on,
                "assignee": default_assignee,
            }
        )

    return tasks


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

    from server.project_store import ProjectStore
    from server.goal_run.models import TaskNode, TaskStatus
    from server.goal_run.store import save_goal_run

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
