"""goal_run wire — 把 goal_run 能力注册进 master_tools。

2026-08-17：只注入 project_run_goal 与 project_goal_status 两个接口。
其它子能力（plan/verify 等）通过现有 project_ask / hicode 链路完成，避免
在 Coordinator 里拆分多套 tool 给模型自主路由。
"""

from typing import Any

from server.goal_run.runner import project_run_goal
from server.goal_run.status import project_goal_status


def wire_master_tools() -> int:
    """把 project_run_goal / project_goal_status 注册进 master_tools。

    幂等：第二次调用返回 0（已注册则跳过）。
    """
    added = 0

    # 检查是否已注册
    from server.tool_registry import master_tools

    if master_tools.has("project_run_goal"):
        return 0  # 已注册，直接返回

    # 注册 project_run_goal
    added += _wire_project_run_goal(master_tools)

    # 注册 project_goal_status（只读）
    added += _wire_project_status(master_tools)

    return added


def _wire_project_run_goal(master_tools: Any) -> int:
    """注册 project_run_goal tool（长时闭环执行）。"""
    import json

    master_tools.register(
        "project_run_goal",
        "Veya 长时目标闭环执行：单一入口吃下复杂目标，自动分解→调度→执行→验收→完成。"
        "遵循 v0.1 Spec：目标层队列（任务图）权威，叶子执行复用 hicode/dsh 既有路径，"
        "无模型 SKU 路由、无影子调度器。返回统一 GoalRunResponse。"
        "参数: project_root, goal, mode(auto|act_eager|ask_only), resume_goal_id, "
        "parent_goal_clarification, max_wall_s, wait(true默认阻塞到终态或超时)。"
        "输出: goal_id, status, phase, interpretation/questions, goal_counts, summary, "
        "block_reason, artifacts, next_action。",
        {
            "type": "object",
            "properties": {
                "project_root": {
                    "type": "string",
                    "description": "项目根目录绝对路径（须位于 .veya-project/ 目录内）。",
                },
                "goal": {
                    "type": "string",
                    "description": "用户复杂目标文本，例如 '实现一个爬虫抓取新闻并存储到数据库'。",
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "act_eager", "ask_only"],
                    "description": "auto(默认): 先澄清再执行; act_eager: 跳过判定直接执行; "
                    "ask_only: 只澄清永不执行。",
                },
                "resume_goal_id": {
                    "type": "string",
                    "description": "可选。若要 resume 一个未完成的 run，传该 goal_id。",
                },
                "parent_goal_clarification": {
                    "type": "string",
                    "description": "可选。G0 追问的回答，用于续答链（避免重复提问）。",
                },
                "max_wall_s": {
                    "type": "integer",
                    "description": "可选。覆盖默认预算 max_wall_s（秒），超时则 goal blocked budget_exceeded。",
                },
                "wait": {
                    "type": "boolean",
                    "description": "可选，默认 true. true: 阻塞到终态或超时; false: 快速返回 running, "
                    "由前端轮询 project_goal_status。",
                },
            },
            "required": ["project_root", "goal"],
        },
        project_run_goal,
        max_result_chars=8000,
    )
    return 1


def _wire_project_status(master_tools: Any) -> int:
    """注册 project_goal_status tool（只读状态查询）。"""
    from server.goal_run.store import load_goal_run

    master_tools.register(
        "project_goal_status",
        "只读查询 goal_run 状态：返回 taskgraph 摘要 + 最近 events 尾部。"
        "不调度、不执行代码、不修改任何文件——诊断/展示用。"
        "参数: project_root, goal_id. 输出: goal_id, status, phase, task_counts, "
        "summary, block_reason, next_action, events 尾部。",
        {
            "type": "object",
            "properties": {
                "project_root": {
                    "type": "string",
                    "description": "项目根目录绝对路径。",
                },
                "goal_id": {
                    "type": "string",
                    "description": "可选。指定 goal_id；若不提供则返回最近一个 run 的状态。",
                },
            },
            "required": ["project_root"],
        },
        project_goal_status,
        max_result_chars=4000,
    )
    return 1
