# 3O-IO-ALLOW bot catalog boundary: BotRegistry persists the bot catalog to one
# JSON file with the same atomic-replace pattern as RoutineRegistry; all
# execution/context/computer state stays in the existing durable stores.
"""Multi-bot identity and isolation enforcement (P3-A).

One user may own several persistent Veya Bots. Every durable object records
the ``bot_id`` of the bot it belongs to, and any cross-bot read / resume /
execute / reuse is refused fail-closed via :class:`CrossBotAccessDenied`.

A bot is identity + configuration only. It owns no execution authority and
no acceptance authority::

    MasterAgent       = semantic authority
    GoalRun           = execution authority
    ComputerSupervisor= physical authority
    SideEffectLedger  = side-effect authority
    IndependentVerifier = acceptance authority
    Bot               = identity / namespace (zero authority)

This module depends on the standard library only so any layer
(``runtime/*``, ``server/*``) can import it without creating cycles.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from runtime.bot_scope import DEFAULT_BOT_ID, CrossBotAccessDenied, require_same_bot


@dataclass(frozen=True)
class BotSpec:
    """Identity and configuration of one persistent Veya Bot.

    Namespaces isolate knowledge/context; the registry refs scope which
    routines the bot may trigger and which skills/playbooks are available
    to it. Availability is an explicit allowlist (fail-closed): an empty
    list allows nothing.
    """

    bot_id: str
    owner_id: str = ""
    config_version: int = 1
    knowledge_namespace: str = ""
    context_namespace: str = ""
    routine_registry_ref: str = ""
    available_skill_ids: list[str] = field(default_factory=list)
    available_playbook_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.bot_id:
            raise ValueError("bot_id is required")
        if not self.knowledge_namespace:
            object.__setattr__(self, "knowledge_namespace", f"knowledge:{self.bot_id}")
        if not self.context_namespace:
            object.__setattr__(self, "context_namespace", f"context:{self.bot_id}")
        if not self.routine_registry_ref:
            object.__setattr__(self, "routine_registry_ref", f"routines:{self.bot_id}")

    @property
    def user_id(self) -> str:
        """Alias: the user who owns this bot."""
        return self.owner_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BotSpec:
        return cls(
            bot_id=str(value.get("bot_id") or ""),
            owner_id=str(value.get("owner_id") or value.get("user_id") or ""),
            config_version=int(value.get("config_version") or 1),
            knowledge_namespace=str(value.get("knowledge_namespace") or ""),
            context_namespace=str(value.get("context_namespace") or ""),
            routine_registry_ref=str(value.get("routine_registry_ref") or ""),
            available_skill_ids=list(value.get("available_skill_ids") or []),
            available_playbook_ids=list(value.get("available_playbook_ids") or []),
        )


def validate_bot_spec(spec: BotSpec) -> list[str]:
    """Validate a bot without creating anything."""
    errors: list[str] = []
    if not spec.bot_id:
        errors.append("bot_id is required")
    if not spec.owner_id:
        errors.append("owner_id is required")
    if spec.config_version < 1:
        errors.append("config_version must be >= 1")
    return errors


def bot_may_use_skill(bot: BotSpec, skill_id: str) -> bool:
    """Whether ``skill_id`` is available to ``bot`` (explicit allowlist)."""
    return skill_id in bot.available_skill_ids


def bot_may_use_playbook(bot: BotSpec, playbook_id: str) -> bool:
    """Whether ``playbook_id`` is available to ``bot`` (explicit allowlist)."""
    return playbook_id in bot.available_playbook_ids


def assert_bot_may_use_skill(bot: BotSpec, skill_id: str) -> None:
    if not bot_may_use_skill(bot, skill_id):
        raise CrossBotAccessDenied(f"skill:{skill_id}", bot.bot_id, f"allowlist:{bot.bot_id}")


def assert_bot_may_use_playbook(bot: BotSpec, playbook_id: str) -> None:
    if not bot_may_use_playbook(bot, playbook_id):
        raise CrossBotAccessDenied(f"playbook:{playbook_id}", bot.bot_id, f"allowlist:{bot.bot_id}")


class BotRegistry:
    """Bot catalog with optional JSON file persistence (mirrors RoutineRegistry)."""

    def __init__(self, storage_path: str | Path | None = None):
        self.storage_path = (
            Path(storage_path)
            if storage_path is not None
            else Path(
                os.environ.get(
                    "VEYA_BOT_REGISTRY_PATH",
                    str(Path.home() / ".veya" / "bot_registry.json"),
                )
            ).expanduser()
        )
        self._items: dict[str, BotSpec] = {}
        self._load()

    def _load(self) -> None:
        if not self.storage_path.exists():
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for bot_id, record in data.items():
            if isinstance(record, dict):
                try:
                    self._items[str(bot_id)] = BotSpec.from_dict(record)
                except ValueError:
                    continue

    def _save(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.storage_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {bid: spec.to_dict() for bid, spec in self._items.items()},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        os.replace(tmp, self.storage_path)

    def register(self, spec: BotSpec) -> None:
        errors = validate_bot_spec(spec)
        if errors:
            raise ValueError(f"invalid bot {spec.bot_id!r}: " + "; ".join(errors))
        self._items[spec.bot_id] = spec
        self._save()

    def get(self, bot_id: str) -> BotSpec | None:
        return self._items.get(bot_id)

    def list(self, *, owner_id: str | None = None) -> list[BotSpec]:
        items = list(self._items.values())
        if owner_id is not None:
            items = [spec for spec in items if spec.owner_id == owner_id]
        return items


__all__ = [
    "DEFAULT_BOT_ID",
    "BotRegistry",
    "BotSpec",
    "CrossBotAccessDenied",
    "assert_bot_may_use_playbook",
    "assert_bot_may_use_skill",
    "bot_may_use_playbook",
    "bot_may_use_skill",
    "require_same_bot",
    "validate_bot_spec",
]
