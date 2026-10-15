"""W11: bot turns show the model's own answer, not the GoalRun report card."""

from types import SimpleNamespace

from server.bot_team.turns import extract_reply


def _bot():
    return SimpleNamespace(name="E2E", bot_id="e2e")


def test_extract_reply_prefers_model_answer_over_run_card():
    resp = {
        "status": "completed",
        "reply": "2+3等于5。",
        "summary": "Goal: x\nStatus: completed\nTasks: 1/1 completed",
    }
    assert extract_reply(resp, bot=_bot()) == "2+3等于5。"


def test_extract_reply_without_answer_keeps_old_behaviour():
    resp = {"status": "completed", "reply": None, "summary": "Goal: x\nStatus: completed"}
    assert extract_reply(resp, bot=_bot()) != ""
