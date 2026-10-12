"""W11-D (S7): a loop brief posts checkpoint cards through a bot turn.

Drive the two things the brief requires end to end, with a single fake of the
exported dispatcher:

1. ``POST /api/v1/bots/{bot}/messages`` with ``loop 2 rounds: …`` runs a bot
   turn whose brief targets a ``FakeGoalRunner`` that **actually** engages the
   loop path, posts a per-round card **and** a finish card into the bot's own
   conversation (the card id lands in the message meta), and reports the ids.
2. ``POST /api/v1/bots/{bot}/checkpoints/{card_id}`` with ``{"verdict":
   "accept"}`` answers that card and records the verdict event.

The routing, the real dispatch and the event/description shaping keep going
through the shared dispatcher, so the fake observes every card the product
would post.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.bot_team.conversations import ConversationStore
from server.events import EventStore
from server.goal_run import runner as runner_mod
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.nano_w10 import loop_mode as loop_mode_mod
from server.routes import bot_team as bot_team_routes
from server.routes.bot_team import router as bots_router
from server.task_store import TaskStore

USER = {"user_id": "w11d-user", "username": "w11d"}

BOT_ID = "loopbot"
GOAL_RUN_ID = "goal-loop-1"

LOOP_TEXT = "loop 2 rounds: 每轮列出一个学习 Python 的建议"


class FakeGoalRunner:
    """Stand-in for :mod:`server.goal_run.runner` that drives the loop once."""

    def __init__(self, *, bot_id: str, goal: str, project_root: str, **kwargs) -> None:
        self.bot_id = bot_id or ""
        self.goal_text = str(goal or "")
        self.project_root = project_root

    async def run(self) -> dict:
        from server.goal_run.models import GoalRunState

        state = GoalRunState(goal_id=GOAL_RUN_ID, goal_text=self.goal_text, bot_id=self.bot_id)
        loop_policy, brief = runner_mod._loop_policy_for(state, self.goal_text)
        if loop_policy is None:
            return {"goal_id": GOAL_RUN_ID, "status": "completed", "cards": []}
        cards: list[str] = []
        for round_index in range(1, int(brief.get("turn_budget", 1)) + 1):
            verdict = loop_policy.decide(
                GOAL_RUN_ID,
                mode="loop",
                round_index=round_index,
                rounds_done=round_index,
                elapsed_s=float(round_index * 10),
                turn_budget=int(brief.get("turn_budget", 1)),
                summary=f"round {round_index} done",
                did=[f"task-{round_index}"],
            )
            card = verdict.get("card")
            if card:
                cards.append(str(card.get("round")))
            if verdict.get("stop"):
                verdict = loop_policy.decide(
                    GOAL_RUN_ID,
                    mode="loop",
                    round_index=round_index,
                    rounds_done=round_index,
                    elapsed_s=float(round_index * 10),
                    turn_budget=int(brief.get("turn_budget", 1)),
                    no_progress_rounds=2,
                    summary=f"loop {round_index} finished",
                    did=[f"task-{round_index}"],
                )
                stop_card = verdict.get("card")
                if stop_card:
                    cards.append(str(stop_card.get("round")))
                break
        return {"goal_id": GOAL_RUN_ID, "status": "completed", "cards": cards}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bots.json"))
    monkeypatch.setenv("VEYA_TASK_STORE_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setenv("VEYA_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    # Prod runs with these on; the brief requires both.
    monkeypatch.setenv("VEYA_LOOP_MODE", "1")
    monkeypatch.setenv("VEYA_FINISH_CHECKPOINT", "1")
    monkeypatch.setattr(loop_mode_mod, "_GATE", None)

    BotRegistry().register(BotSpec(bot_id=BOT_ID, owner_id=USER["user_id"], name="Loop Bot"))

    from server.routes import product as product_routes

    class _History:
        async def save(self, *args, **kwargs) -> None:
            return None

    events = EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr(
        product_routes, "task_store", TaskStore(tmp_path / "tasks.json", event_store=events)
    )
    monkeypatch.setattr(product_routes, "default_history_store", lambda: _History())

    app = FastAPI()
    app.dependency_overrides[auth_mod.require_user] = lambda: dict(USER)
    app.include_router(bots_router)
    return TestClient(app)


@pytest.fixture
def fake_master(monkeypatch):
    """Turn the real product path into one real loop, executed here."""

    async def fake_execute(request):
        from server.goal_run.models import GoalRunResponse, GoalStatus

        goal_text = getattr(request, "objective", "") or ""
        project_root = getattr(request, "project_root", ".")
        bot_id = str((request.constraints.metadata or {}).get("bot_id") or "")
        runner = FakeGoalRunner(bot_id=bot_id, goal=goal_text, project_root=project_root)
        result = await runner.run()
        return GoalRunResponse(
            goal_id=result["goal_id"],
            status=GoalStatus.completed,
            phase="finalized",
            reply="每轮建议已列出。",
            summary="loop cards posted",
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)
    yield


def test_loop_brief_posts_cards_then_accepts(env, fake_master, monkeypatch):
    """S7: loop brief → cards in conversation → verdict accepted (200)."""

    resp = env.post(f"/api/v1/bots/{BOT_ID}/messages", json={"text": LOOP_TEXT})
    assert resp.status_code == 200, resp.text
    assert resp.json()["reply"]

    store = ConversationStore()
    messages = store.list(BOT_ID, include_hidden=True)

    # Every posted card lands in the bot's own conversation with its card id.
    cards = [m for m in messages if (m.get("meta") or {}).get("kind") == "finish_checkpoint"]
    assert cards, "no checkpoint card reached the conversation"
    card_ids = {(m["meta"] or {}).get("card_id") for m in cards}
    assert len(card_ids) == len(cards)  # one card per round, no duplicates
    assert all(r.startswith("fc-") for r in card_ids)

    # A per-round card, not just one finish card.
    per_round = {(m["meta"] or {}).get("round") for m in cards}
    assert 1 in per_round and 2 in per_round

    # The verdict route answers any one of the posted cards.
    target = next(iter(card_ids))
    verdict = env.post(
        f"/api/v1/bots/{BOT_ID}/checkpoints/{target}",
        json={"verdict": "accept"},
    )
    assert verdict.status_code == 200, verdict.text
    assert verdict.json()["verdict"] == "accept"
    assert verdict.json()["card_id"] == target


def test_loop_cards_are_refused_when_not_in_that_conversation(env):
    """A card id nobody posted answers 404, not 200."""

    resp = env.post(
        f"/api/v1/bots/{BOT_ID}/checkpoints/fc-guessed-id",
        json={"verdict": "accept"},
    )
    assert resp.status_code == 404
