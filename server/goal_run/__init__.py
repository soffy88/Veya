"""goal_run — 复杂目标的长时闭环执行 (M4: project_run_goal)。

Veya project_run_goal Spec v0.1:
- 单一入口吃下复杂目标，自动（或半自动）跑到可交付状态或明确 blocked
- 任务图持久化在项目内，可 resume、可审计
- 工人默认同构（hicode / dsh 由配置选定），并行靠任务依赖与隔离
- 每步有验收；不过则返工有上限，最终 completed 或 blocked
- 遵守现有纪律：不靠多 tool 做意图路由；不平行第二套与 HicodeTaskQueue 无关的「影子调度器」
"""

from server.goal_run.runner import cancel_goal, project_run_goal
from server.goal_run.status import project_goal_status
from server.goal_run.wire import wire_master_tools


def project_run_goal_boss_mode(*args, **kwargs):
    """Load the optional Boss adapter only when that adapter is requested."""
    from server.boss_entrypoint import project_run_goal_boss_mode as _run_boss

    return _run_boss(*args, **kwargs)


__all__ = [
    "cancel_goal",
    "project_goal_status",
    "project_run_goal",
    "project_run_goal_boss_mode",
    "wire_master_tools",
]
