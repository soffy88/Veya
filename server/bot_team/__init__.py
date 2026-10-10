"""Lorca bot team: named bots, their conversations, group chats (P1/P2) and per-bot Markdown memory (P3).

A bot is identity + configuration only (see ``server/goal_run/bot_identity.py``).
This package adds the conversation surface and the turn/group turn runs. It owns
no execution authority and no task store: every bot turn goes through the
existing product path (``server/routes/product.py`` → ``master_coordinator``
→ GoalRun), which is what makes a bot turn a real task.
"""

from __future__ import annotations

from server.bot_team.conversations import (
    PASS_SENTINEL,
    BotMessage,
    ConversationStore,
    conversation_store,
    default_bots_root,
)
from server.bot_team.groups import MAX_GROUP_MEMBERS, GroupChat, GroupStore
from server.bot_team.memory import BotMemoryStore
from server.bot_team.messaging import (
    MAX_HOP_DEPTH,
    BotMessageRefused,
    BotMessenger,
    message_bot,
)
from server.bot_team.turns import BotTurnResult, build_objective, run_bot_turn

__all__ = [
    "MAX_GROUP_MEMBERS",
    "MAX_HOP_DEPTH",
    "PASS_SENTINEL",
    "BotMessage",
    "BotMessageRefused",
    "BotMemoryStore",
    "BotMessenger",
    "BotTurnResult",
    "ConversationStore",
    "GroupChat",
    "GroupStore",
    "build_objective",
    "conversation_store",
    "default_bots_root",
    "message_bot",
    "run_bot_turn",
]
