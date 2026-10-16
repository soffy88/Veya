from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.routes.bot_team import CHEF_BOT_ID, DEFAULT_BOT_ID, _resolve_bot


def test_legacy_owner_markers_are_not_shared_grants():
    registry = BotRegistry()
    for bot_id, owner in (
        ("legacy-system", "system"),
        ("legacy-default", "default"),
        ("legacy-anonymous", "anonymous"),
    ):
        registry.register(BotSpec(bot_id=bot_id, owner_id=owner, name=bot_id))
        assert _resolve_bot(registry, bot_id, owner_id="alice") is None
        assert _resolve_bot(registry, bot_id, owner_id="") is None

    class LegacyRegistry:
        def get(self, bot_id):
            return BotSpec(bot_id=bot_id, owner_id="", name="legacy-empty")

    assert _resolve_bot(LegacyRegistry(), "legacy-empty", owner_id="alice") is None
    assert _resolve_bot(LegacyRegistry(), "legacy-empty", owner_id="") is None


def test_owned_bot_requires_exact_owner_and_builtins_remain_shared():
    registry = BotRegistry()
    registry.register(BotSpec(bot_id="alice-bot", owner_id="alice", name="Alice"))
    registry.register(BotSpec(bot_id=DEFAULT_BOT_ID, owner_id="system", name="Default"))
    registry.register(BotSpec(bot_id=CHEF_BOT_ID, owner_id="system", name="Chef"))
    assert _resolve_bot(registry, "alice-bot", owner_id="alice") is not None
    assert _resolve_bot(registry, "alice-bot", owner_id="bob") is None
    assert _resolve_bot(registry, "alice-bot", owner_id="") is None
    assert _resolve_bot(registry, DEFAULT_BOT_ID, owner_id="bob") is not None
    assert _resolve_bot(registry, CHEF_BOT_ID, owner_id="bob") is not None
