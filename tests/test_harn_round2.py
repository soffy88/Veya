"""Tests for round 2 HARN S3 language constraint fix."""

from server.bot_team.turns import build_objective
from server.goal_run.bot_identity import BotSpec


def test_build_objective_includes_chinese_response_instruction():
    bot = BotSpec(bot_id="b1", name="Bot")
    obj = build_objective(bot=bot, text="请一句话复述。")
    assert "请使用中文" in obj
