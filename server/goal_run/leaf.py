"""goal_run leaf — 叶子任务执行适配层（复用 project_ask 执行路径）。

硬规则：禁止复制一套 dsh/hicode 调用；抽 execute_leaf(project_root, instruction, assignee) -> LeafResult
供 project_ask act 与 goal runner 共用（可小重构）。

返回 LeafResult：{
    "status": "completed"|"blocked",
    "summary": str,
    "block_reason": str | None,
    "artifacts": list[str]
}
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass
class LeafResult:
    """叶子执行结果。"""

    status: str  # "completed" | "blocked"
    summary: str
    block_reason: str | None = None
    artifacts: list[str] = None

    def __post_init__(self):
        if self.artifacts is None:
            self.artifacts = []


async def execute_leaf(
    project_root: str,
    instruction: str,
    acceptance: list[str] | None = None,
    assignee: str = "hicode",
    memory_prefix: str = "",
) -> LeafResult:
    """执行单个叶子任务，复用 project_ask 内部执行路径。

    实现要点：
    - assignee=hicode → 调用 project_ask 的 _run_hicode 路径（force_cli=True）
    - assignee=dsh → 调用 project_ask 的 _run_dsh 路径
    - assignee=builtin → 不执行，直接返回 blocked（goal_run 中不应有 builtin 任务）
    - 组装 instruction = 任务 instruction + acceptance 列表 + 项目记忆前缀
    - 返回 LeafResult
    """
    from server.capability_model import harness_registry
    from server.project_store import ProjectStore

    store = ProjectStore(project_root)
    store.ensure_layout()

    # 组装 brief（类似 project_ask 的 brief 风格）
    parts = [p for p in (memory_prefix, _context_prefix(store)) if p]
    if acceptance:
        acceptance_text = "\n".join(f"- {a}" for a in acceptance)
        parts.append(f"## Acceptance 条件\n{acceptance_text}")
    parts.append(f"## Task\n{instruction}")

    brief = "\n\n".join(parts)
    task_id = f"leaf_{instruction[:30].replace(' ', '_')}"

    # 运行目录
    run_dir = store.run_dir(task_id)
    (run_dir / "brief.md").write_text(brief, encoding="utf-8")

    # 派工执行。经 HarnessRegistry.execute() 路由(PR-15, 见
    # server/capability_model.py::HarnessRegistry.execute 的 docstring)——
    # _run_builtin/_run_hicode/_run_dsh 本身零改动, 参数/返回值跟直接调用完全一致。
    if assignee == "builtin":
        resp = await harness_registry.execute(
            "builtin", store=store, task_id=task_id, request=instruction
        )
        return LeafResult(
            status="completed" if resp.status == "completed" else "blocked",
            summary=resp.summary or "",
            block_reason=resp.block_reason,
            artifacts=resp.artifacts,
        )
    elif assignee == "hicode":
        resp = await harness_registry.execute(
            "hicode", store=store, task_id=task_id, request=instruction, project_root=project_root
        )
        return LeafResult(
            status=resp.status,
            summary=resp.summary or "",
            block_reason=resp.block_reason,
            artifacts=resp.artifacts,
        )
    elif assignee == "dsh":
        resp = await harness_registry.execute(
            "dsh", store=store, task_id=task_id, request=instruction, project_root=project_root
        )
        return LeafResult(
            status=resp.status,
            summary=resp.summary or "",
            block_reason=resp.block_reason,
            artifacts=resp.artifacts,
        )
    else:
        return LeafResult(
            status="blocked",
            summary="",
            block_reason=f"unknown assignee: {assignee}",
            artifacts=[],
        )


def _context_prefix(store: ProjectStore) -> str:
    """拼装项目记忆作为派工上下文前缀（复用 project_ask._context_prefix）。"""
    state = store.read_state()[:2000]
    decisions = store.read_decisions()[-1500:]
    lessons = store.read_lessons()[-1500:]
    parts = [f"## Authoritative project memory (.veya-project/)\n\n### PROJECT_STATE.md\n{state}"]
    if decisions.strip():
        parts.append(f"### DECISIONS.md (recent)\n{decisions}")
    if lessons.strip():
        parts.append(f"### LESSONS.md (recent)\n{lessons}")
    return "\n\n".join(parts)


# ── 便捷函数：从 project_ask 复用的执行入口（供 goal_run 调用） ──────────────


async def execute_leaf_with_memory(
    project_root: str,
    instruction: str,
    acceptance: list[str] | None = None,
    assignee: str = "hicode",
    constitution_text: str = "",
) -> LeafResult:
    """带完整项目记忆的叶子执行（goal_run 专用入口）。

    实现要点：
    - 自动拼装项目记忆前缀
    - 传入 acceptance 作为验收条件
    - 返回 LeafResult
    """
    from server.project_store import ProjectStore

    store = ProjectStore(project_root)
    store.ensure_layout()
    memory = _context_prefix(store)
    if constitution_text.strip():
        from oprim._jailed_executor import compose_constitution_brief

        instruction = compose_constitution_brief(
            instruction=instruction, constitution_text=constitution_text
        )

    return await execute_leaf(
        project_root=project_root,
        instruction=instruction,
        acceptance=acceptance,
        assignee=assignee,
        memory_prefix=memory,
    )
