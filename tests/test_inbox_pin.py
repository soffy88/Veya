"""Inbox pinning: open escalations sort above ordinary questions."""

from __future__ import annotations

from server.bot_team.attention import AttentionStore, build_inbox
from server.bot_team.conversations import ConversationStore
from server.bot_team.groups import GroupStore
from server.goal_run.bot_identity import BotRegistry


def test_escalations_pin_above_questions(tmp_path):
    root = tmp_path / "bots-root"
    store = AttentionStore(root)
    store.create(question="ordinary one", kind="question")
    store.create(question="stuck round", kind="escalation")
    store.create(question="ordinary two", kind="question")
    store.create(question="failed job", kind="incident")
    inbox = build_inbox(
        BotRegistry(),
        conversations=ConversationStore(root),
        groups=GroupStore(root),
        store=store,
    )
    kinds = [item.get("kind") for item in inbox["attention"]]
    assert kinds == ["escalation", "question", "question", "incident"]
