"""W11 S2/S3: a page read tolerates `url=`; a burst of messages is one answer."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.asyncio
async def test_read_channel_accepts_url_alias(monkeypatch):
    import server.channel_tools as ct

    monkeypatch.setenv("VEYA_CHANNEL_LADDER", "1")

    async def fake_probe(ch):
        return {"status": "down", "fix_hint": "no backend"}

    async def fake_fetch(url, timeout=10.0, user_agent=""):
        return "<html><head><title>Example Domain</title></head><body><h1>Example Domain</h1></body></html>"

    monkeypatch.setattr(ct, "probe_channel", fake_probe)
    monkeypatch.setattr(ct, "fetch_url_text", fake_fetch)
    out = await ct.tool_read_channel(url="https://example.com", category="web")
    assert "Example Domain" in out


@pytest.mark.asyncio
async def test_burst_of_messages_is_answered_in_one_run(monkeypatch, tmp_path):
    import asyncio

    from server.bot_team import turns as turns_mod
    from server.goal_run.bot_identity import BotSpec
    from server.nano_w10 import turn_queue as tq
    from server.routes import product as product_routes

    monkeypatch.setenv("VEYA_TURN_QUEUE", "1")
    monkeypatch.setenv("VEYA_TURN_COALESCE_S", "0.5")
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots"))
    queue = tq.get_turn_queue()
    bot = BotSpec(bot_id="bot_s3_unit", owner_id="u1", name="S3", runner_id="local")
    objectives: list[str] = []

    async def fake_open(**kwargs: Any) -> tuple[str, str]:
        return "s_s3", "t_s3"

    async def fake_run(**kwargs: Any) -> dict[str, Any]:
        objectives.append(kwargs["objective"])
        return {"status": "completed", "reply": "小林，杭州。"}

    monkeypatch.setattr(product_routes, "_open_task_projection", fake_open)
    monkeypatch.setattr(product_routes, "_run_product_task", fake_run)

    async def later_pieces():
        await asyncio.sleep(0.1)
        queue.steer(bot.bot_id, "第二条：我住在杭州。")
        queue.steer(bot.bot_id, "第三条：请复述。")

    pieces = asyncio.create_task(later_pieces())
    result = await turns_mod.run_bot_turn(bot=bot, text="第一条：我叫小林。", user={"user_id": "u1"})
    await pieces
    assert len(objectives) == 1, "a burst must cost one model run"
    assert "杭州" in objectives[0] and "复述" in objectives[0]
    assert "杭州" in result.reply
