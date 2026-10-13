"""BotChatService — live Bot chat entry over the real execution control plane (B3).

The canonical chat chain is real end-to-end. Nothing in GoalRun, Local2,
memory or the receipt is mocked:

    bot.chat
      -> resolve bot + conversation + session (product stores)
      -> persist the user message
      -> semantic step (MasterCoordinator seam; injectable)
      -> shared execution chain (bot_runtime.execution.execute_bot_run)
         = GoalRun -> ExecutionAdmission -> ExecutionControl -> Local2
           -> ExecutionActivity -> ExecutionReceipt -> MemoryController
      -> persist the bot response

Authority boundary: the semantic step is the only injectable seam. Everything
downstream of it is the real durable execution chain, shared with
``BotRuntime.submit`` so there is exactly one Bot-side execution path.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from server.bot_runtime.execution import execute_bot_run
from server.bot_runtime.manager import BotManager
from server.bot_runtime.product import BotConversation, BotProduct

logger = logging.getLogger("veya.bot_runtime.chat")

#: Semantic seam: (message, context) -> (response_text, execution_command).
#: The default treats the message as a Local2 objective and returns no
#: pre-computed response (the command's stdout becomes the response).
SemanticFn = Callable[[str, dict[str, Any]], Awaitable[tuple[str, str]]]


async def _default_semantic(message: str, context: dict[str, Any]) -> tuple[str, str]:
    return "", message


@dataclass
class BotChatResult:
    bot_id: str
    conversation_id: str
    session_id: str
    goal_run_id: str
    execution_id: str
    response: str
    status: str
    receipt: dict[str, Any] = field(default_factory=dict)
    memory_id: str | None = None
    activity_sequence: int = 0
    user_message_id: str = ""
    bot_message_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "goal_run_id": self.goal_run_id,
            "execution_id": self.execution_id,
            "response": self.response,
            "status": self.status,
            "receipt": self.receipt,
            "memory_id": self.memory_id,
            "activity_sequence": self.activity_sequence,
            "user_message_id": self.user_message_id,
            "bot_message_id": self.bot_message_id,
        }


class BotChatService:
    """Live Bot chat over the canonical GoalRun / Local2 control plane."""

    def __init__(
        self,
        manager: BotManager,
        project_root: str,
        *,
        product: BotProduct | None = None,
        memory: Any | None = None,
        semantic_fn: SemanticFn | None = None,
        executor: str = "local2",
        admission_executor: str = "codex",
    ) -> None:
        self.manager = manager
        self.project_root = project_root
        self.product = product or getattr(manager, "_product", None) or BotProduct()
        self._memory = memory
        self._semantic = semantic_fn or _default_semantic
        self.executor = executor
        # C2.1 — two roles, formally modelled: ``executor`` is the physical
        # substrate (local2 / native_substrate), ``admission_executor`` is the
        # admitted executor (ExecutorRegistry authority). They differ for a
        # native substrate and are recorded separately in binding and receipt.
        self.admission_executor = admission_executor

    # ── helpers ─────────────────────────────────────────────────────

    def _ensure_conversation(self, bot_id: str, conversation_id: str | None) -> BotConversation:
        if conversation_id:
            return self.product.conversations.require(bot_id, conversation_id)
        main = self.product.conversations.main(bot_id)
        if main is None:
            main = self.product.conversations.create(bot_id, type="main", title="Main")
        return main

    # ── chat ────────────────────────────────────────────────────────

    async def chat(
        self,
        bot_id: str,
        message: str,
        *,
        conversation_id: str | None = None,
    ) -> BotChatResult:
        spec = self.manager.get_bot(bot_id)
        if spec is None:
            raise ValueError(f"unknown bot: {bot_id!r}")

        conversation = self._ensure_conversation(bot_id, conversation_id)

        # Persist the user message first (durable before execution).
        user_message = self.product.messages.append(
            bot_id, conversation.conversation_id, sender="user", content=message
        )

        # Bind a bot session to the conversation.
        computer = self.manager.ensure_computer(bot_id)
        instance_id = getattr(computer, "runtime_id", "") or ""
        session = self.product.sessions.create(
            bot_id, conversation_id=conversation.conversation_id, instance_id=instance_id
        )

        # Semantic step (MasterCoordinator seam).
        response_text, command = await self._semantic(
            message, {"bot_id": bot_id, "conversation_id": conversation.conversation_id}
        )

        # Real GoalRun chain (shared with BotRuntime.submit).
        outcome = await execute_bot_run(
            project_root=self.project_root,
            bot_id=bot_id,
            objective=command or message,
            session_id=session.session_id,
            dispatch_id=f"chat-{uuid.uuid4().hex[:12]}",
            executor=self.executor,
            admission_executor=self.admission_executor,
            memory=self._memory,
            memory_content=f"[bot {bot_id}] user: {message}",
        )

        # Response: semantic response if provided, else the execution stdout.
        final_response = response_text or outcome.stdout
        if not final_response:
            final_response = outcome.reason or f"[{outcome.status}]"

        bot_message = self.product.messages.append(
            bot_id, conversation.conversation_id, sender="bot", content=final_response
        )

        # Link the GoalRun index into the conversation.
        self._link_goal(conversation, outcome.goal_run_id)

        return BotChatResult(
            bot_id=bot_id,
            conversation_id=conversation.conversation_id,
            session_id=session.session_id,
            goal_run_id=outcome.goal_run_id,
            execution_id=outcome.execution_id,
            response=final_response,
            status=outcome.status,
            receipt=outcome.receipt,
            memory_id=outcome.memory_id,
            activity_sequence=outcome.activity_sequence,
            user_message_id=user_message.message_id,
            bot_message_id=bot_message.message_id,
        )

    def _link_goal(self, conversation: BotConversation, goal_run_id: str) -> None:
        conversation.active_goal_ids.append(goal_run_id)
        conversation.updated_at = datetime.now(UTC).isoformat()
        conversations = self.product.conversations._all(conversation.bot_id)
        for item in conversations:
            if item.conversation_id == conversation.conversation_id:
                item.active_goal_ids = conversation.active_goal_ids
                item.updated_at = conversation.updated_at
        self.product.conversations._write(
            conversation.bot_id,
            "conversations.json",
            [c.to_dict() for c in conversations],
        )


__all__ = ["BotChatResult", "BotChatService", "SemanticFn"]
