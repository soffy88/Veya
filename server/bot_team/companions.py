"""Companions: bots with a persona, memory namespaces, and routine work.

A companion is a normal BotSpec (``kind="companion"``) created from a preset
in one call: persona + instructions + the preset's routine templates
instantiated into the bot's own routine registry. A companion is usable
everywhere a bot is: turns, rooms, channels.

Skills stay fail-closed: a preset claims no skill ids. The owner adds them
afterwards through the normal skill allowlist.
"""

from __future__ import annotations

from typing import Any

from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.routine_registry import RoutineRegistry
from server.goal_run.routine_templates import build_template_spec

COMPANION_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "researcher",
        "name": "研究员",
        "personality": "好奇、严谨、只写可验证的事实，凡是没有来源的说法都标出来。",
        "description": "每天盯领域与竞品动态，周一给对照表。",
        "instructions": (
            "你是研究员。每天搜集负责领域的新动态，每周追踪竞品变化；"
            "只写可验证的事实并附来源；不确定的事明说不知道，不编造。"
        ),
        "routine_templates": ["domain-radar", "competitor-watch"],
    },
    {
        "id": "secretary",
        "name": "管家",
        "personality": "细致、准时、只报主人需要拍板的事，不啰嗦。",
        "description": "周五起草周报，周日检查知识库是否过期。",
        "instructions": (
            "你是管家。每周五根据本周任务记录起草周报，标出需要主人确认的部分；"
            "每周日检查知识库条目是否过期、矛盾或缺来源，只列清单、不直接改库。"
        ),
        "routine_templates": ["weekly-digest", "knowledge-freshness"],
    },
    {
        "id": "keeper",
        "name": "守夜人",
        "personality": "沉默寡言、只在出问题时开口，报问题必带证据。",
        "description": "凌晨自愈检查，工作日早晨守 PR 与 CI。",
        "instructions": (
            "你是守夜人。每天凌晨对默认工作区做自愈检查，工作日早晨检查 PR 与 CI；"
            "只在出问题时汇报，汇报必带证据；绝不擅自改代码、合代码。"
        ),
        "routine_templates": ["nightly-self-heal", "pr-gatekeeper"],
    },
)

_BY_ID = {p["id"]: p for p in COMPANION_PRESETS}


def list_presets() -> list[dict[str, Any]]:
    """Public view of every companion preset."""

    return [dict(p) for p in COMPANION_PRESETS]


def get_preset(preset_id: str) -> dict[str, Any]:
    """Return one preset by id, or raise KeyError for an unknown id."""

    preset = _BY_ID.get(str(preset_id or ""))
    if preset is None:
        raise KeyError(f"unknown companion preset: {preset_id!r}")
    return preset


def create_companion(
    registry: BotRegistry,
    routines: RoutineRegistry,
    *,
    owner_id: str,
    preset_id: str,
    bot_id: str,
    name: str | None = None,
    runner_id: str = "local",
) -> tuple[BotSpec, list[str]]:
    """Register a companion bot and instantiate its preset routines.

    Refuses a duplicate bot_id instead of overwriting it. Returns the bot
    spec and the routine ids created for it.
    """

    preset = get_preset(preset_id)
    if registry.get(bot_id) is not None:
        raise ValueError(f"bot {bot_id!r} already exists")
    spec = BotSpec(
        bot_id=bot_id,
        owner_id=owner_id,
        name=(name or preset["name"]).strip(),
        description=str(preset["description"]),
        instructions=str(preset["instructions"]),
        personality=str(preset["personality"]),
        kind="companion",
        runner_id=runner_id,
    )
    registry.register(spec)
    routine_ids: list[str] = []
    for template_id in preset["routine_templates"]:
        routine = build_template_spec(template_id, bot_id)
        routines.register(routine)
        routine_ids.append(routine.routine_id)
    return spec, routine_ids


__all__ = [
    "COMPANION_PRESETS",
    "create_companion",
    "get_preset",
    "list_presets",
]
