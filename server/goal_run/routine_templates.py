"""Builtin routine templates: one-click starting points for a bot's routines.

A template is a fixed, reviewed RoutineSpec (schedule + goal template) that a
user instantiates into one bot's registry. Templates never execute anything
themselves; execution still goes through the normal routine dispatch path.
Instantiating copies the template verbatim (no parameters in v1) under a
fresh routine_id owned by the calling bot.
"""

from __future__ import annotations

import uuid
from typing import Any

from server.goal_run.models import RoutineSpec

TEMPLATE_CATEGORIES = (
    ("ops", "运维"),
    ("dev", "开发"),
    ("info", "信息"),
    ("office", "办公"),
    ("knowledge", "知识"),
)

ROUTINE_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "nightly-self-heal",
        "name": "夜间自愈检查",
        "category": "ops",
        "description": "每天凌晨对默认工作区做一次自愈检查，只汇报、不改代码。",
        "schedule": "0 2 * * *",
        "timezone": "Asia/Shanghai",
        "objective": (
            "对默认工作区做一次夜间自愈检查：跑通核心测试与健康端点，"
            "汇总失败项与可疑日志，只汇报、不改代码、不发版。"
        ),
    },
    {
        "id": "pr-gatekeeper",
        "name": "PR 守门人",
        "category": "dev",
        "description": "工作日早晨检查打开的 PR 与 CI 状态，只汇报、不合并、不评论。",
        "schedule": "0 9 * * 1-5",
        "timezone": "Asia/Shanghai",
        "objective": (
            "检查打开的 PR 与 CI 状态：列出 CI 失败、评审未通过、"
            "超过 3 天无人理的 PR，只汇报、不合并、不评论。"
        ),
    },
    {
        "id": "domain-radar",
        "name": "领域雷达",
        "category": "info",
        "description": "每天早晨搜集负责领域过去 24 小时的新动态，给一页摘要。",
        "schedule": "0 8 * * *",
        "timezone": "Asia/Shanghai",
        "objective": (
            "搜集你负责领域过去 24 小时的新动态（发布、公告、重要讨论），"
            "去重后给一页摘要，附来源，不做判断只陈述。"
        ),
    },
    {
        "id": "competitor-watch",
        "name": "竞品动态追踪",
        "category": "info",
        "description": "每周一追踪主要竞品过去一周的公开动态，整理成对照表。",
        "schedule": "0 8 * * 1",
        "timezone": "Asia/Shanghai",
        "objective": (
            "追踪主要竞品过去一周的公开动态（发布、定价、功能变化），"
            "整理成对照表，只写可验证的事实。"
        ),
    },
    {
        "id": "weekly-digest",
        "name": "周报草稿助手",
        "category": "office",
        "description": "每周五傍晚根据本周任务记录起草周报，标出需主人确认的部分。",
        "schedule": "0 18 * * 5",
        "timezone": "Asia/Shanghai",
        "objective": (
            "根据本周的任务记录与对话，起草一份周报：完成了什么、"
            "进行中什么、卡点是什么，标出需要主人确认的部分。"
        ),
    },
    {
        "id": "knowledge-freshness",
        "name": "知识库保鲜",
        "category": "knowledge",
        "description": "每周日检查知识库条目是否过期、矛盾或缺来源，只列清单、不改库。",
        "schedule": "0 7 * * 0",
        "timezone": "Asia/Shanghai",
        "objective": (
            "检查知识库条目：标出过期、矛盾或缺来源的条目，"
            "列出建议更新清单，不直接改库。"
        ),
    },
)

_BY_ID = {t["id"]: t for t in ROUTINE_TEMPLATES}


def list_templates() -> list[dict[str, Any]]:
    """Public view of every builtin template (no execution state)."""

    return [dict(t) for t in ROUTINE_TEMPLATES]


def get_template(template_id: str) -> dict[str, Any]:
    """Return one template by id, or raise KeyError for an unknown id."""

    template = _BY_ID.get(str(template_id or ""))
    if template is None:
        raise KeyError(f"unknown routine template: {template_id!r}")
    return template


def build_template_spec(
    template_id: str,
    bot_id: str,
    *,
    routine_id: str | None = None,
) -> RoutineSpec:
    """Build the RoutineSpec a template instantiates into one bot's registry."""

    template = get_template(template_id)
    return RoutineSpec(
        routine_id=routine_id or uuid.uuid4().hex,
        trigger_topic="schedule.trigger",
        objective=str(template["objective"]),
        goal_template=str(template["objective"]),
        schedule=str(template["schedule"]),
        timezone=str(template["timezone"]),
        enabled=True,
        bot_id=str(bot_id),
        probe={"kind": "none"},
    )


__all__ = [
    "ROUTINE_TEMPLATES",
    "TEMPLATE_CATEGORIES",
    "build_template_spec",
    "get_template",
    "list_templates",
]
