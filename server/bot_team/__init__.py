"""Lorca bot team: named bots, their conversations, group chats (P1/P2) and per-bot Markdown memory (P3).

A bot is identity + configuration only (see ``server/goal_run/bot_identity.py``).
This package adds the conversation surface and the turn/group turn runs. It owns
no execution authority and no task store: every bot turn goes through the
existing product path (``server/routes/product.py`` → ``master_coordinator``
→ GoalRun), which is what makes a bot turn a real task.

Lorca (P6) adds where that turn may run: a runner registry plus a forwarding
client, so a bot can execute on another machine while this server keeps the
catalog and the conversations.
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
from server.bot_team.runner_client import (
    RunnerClient,
    RunnerTransport,
    forward_bot_turn,
)
from server.bot_team.runners import (
    LOCAL_RUNNER_ID,
    RunnerError,
    RunnerRecord,
    RunnerRegistry,
    runner_registry,
)
from server.bot_team.turns import BotTurnResult, build_objective, run_bot_turn

__all__ = [
    "LOCAL_RUNNER_ID",
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
    "RunnerClient",
    "RunnerError",
    "RunnerRecord",
    "RunnerRegistry",
    "RunnerTransport",
    "build_objective",
    "conversation_store",
    "default_bots_root",
    "forward_bot_turn",
    "message_bot",
    "run_bot_turn",
    "runner_registry",
]
