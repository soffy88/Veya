"""W11 D2: a bot is private to its owner (supervisor regression test)."""

from __future__ import annotations

from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.routes import bot_team


def _registry(tmp_path):
    reg = BotRegistry(tmp_path / "bot_registry.json")
    reg.register(BotSpec(bot_id="alice_bot", owner_id="alice", name="A"))
    reg.register(BotSpec(bot_id="shared_bot", owner_id="system", name="S"))
    return reg


def test_owner_sees_own_bot(tmp_path):
    assert bot_team._resolve_bot(_registry(tmp_path), "alice_bot", owner_id="alice") is not None


def test_other_user_gets_none(tmp_path):
    assert bot_team._resolve_bot(_registry(tmp_path), "alice_bot", owner_id="bob") is None


def test_shared_bot_visible(tmp_path):
    assert bot_team._resolve_bot(_registry(tmp_path), "shared_bot", owner_id="bob") is not None
