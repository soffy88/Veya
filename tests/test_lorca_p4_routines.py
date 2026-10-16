"""P4 tests: scheduled/proactive bot tasks (routine clock + REST)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.bot_team.routine_clock import RoutineClock, compute_due
from server.goal_run.models import RoutineSpec
from server.goal_run.routine_registry import RoutineRegistry
from server.routes.routines import router


@pytest.fixture
def test_app(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routines.json"))
    app = FastAPI()
    # W11 D2: routine routes check bot ownership; these tests cover routine logic.
    from server import auth as auth_mod
    from server.routes import routines as _routines_mod

    app.dependency_overrides[_routines_mod._require_owned_bot] = lambda: None
    app.dependency_overrides[auth_mod.require_user] = lambda: {
        "user_id": "u1",
        "username": "test-owner",
        "is_owner": True,
    }
    app.include_router(router)
    return app


def test_cron_due_computation():
    now = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
    last_fired = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    spec = RoutineSpec(
        routine_id="r1",
        trigger_topic="schedule.trigger",
        objective="test",
        goal_template="test",
        bot_id="b1",
        schedule="0 9 * * *",
        timezone="UTC",
    )
    due = compute_due(spec, now=now, last_fired_utc=last_fired)
    assert due == "2026-01-01T09:00:00+00:00"


def test_interval_parsing():
    now = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    last_fired = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    spec = RoutineSpec(
        routine_id="r1",
        trigger_topic="schedule.trigger",
        objective="test",
        goal_template="test",
        bot_id="b1",
        schedule="every 30m",
    )
    due = compute_due(spec, now=now, last_fired_utc=last_fired)
    # The catch-up gives most recent past occurrence
    assert due == "2026-01-01T10:00:00+00:00"


def test_daily_time_parsing():
    now = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    last_fired = datetime(2025, 12, 31, 8, 0, tzinfo=UTC)
    spec = RoutineSpec(
        routine_id="r1",
        trigger_topic="schedule.trigger",
        objective="test",
        goal_template="test",
        bot_id="b1",
        schedule="daily 09:00",
        timezone="UTC",
    )
    due = compute_due(spec, now=now, last_fired_utc=last_fired)
    assert due == "2026-01-01T09:00:00+00:00"


def test_timezone_support():
    now = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)  # 18:00 Asia/Shanghai
    last_fired = datetime(2025, 12, 31, 8, 0, tzinfo=UTC)
    # 09:00 in Asia/Shanghai is 01:00 UTC
    spec = RoutineSpec(
        routine_id="r1",
        trigger_topic="schedule.trigger",
        objective="test",
        goal_template="test",
        bot_id="b1",
        schedule="daily 09:00",
        timezone="Asia/Shanghai",
    )
    due = compute_due(spec, now=now, last_fired_utc=last_fired)
    assert due == "2026-01-01T01:00:00+00:00"


@pytest.mark.asyncio
async def test_no_double_fire_across_restart(tmp_path):
    registry = RoutineRegistry(bot_id="b1", storage_path=str(tmp_path / "reg.json"))
    spec = RoutineSpec(
        routine_id="r1",
        trigger_topic="schedule.trigger",
        objective="test",
        goal_template="test",
        bot_id="b1",
        schedule="every 30m",
        enabled=True,
    )
    registry.register(spec)

    clock = RoutineClock(registry, clock_path=str(tmp_path / "clock.json"), dispatch_fn=AsyncMock())

    with patch("server.bot_team.routine_clock.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        mock_dt.fromisoformat = datetime.fromisoformat

        # first tick initializes last_fired to "now"
        handoffs = await clock.tick()
        assert len(handoffs) == 0

        # fast forward 40 mins
        mock_dt.now.return_value = datetime(2026, 1, 1, 10, 40, tzinfo=UTC)
        handoffs = await clock.tick()
        assert len(handoffs) == 1

        # tick again without time change
        handoffs = await clock.tick()
        assert len(handoffs) == 0


@pytest.mark.asyncio
async def test_catchup_one_occurrence(tmp_path):
    registry = RoutineRegistry(bot_id="b1", storage_path=str(tmp_path / "reg.json"))
    spec = RoutineSpec(
        routine_id="r1",
        trigger_topic="schedule.trigger",
        objective="test",
        goal_template="test",
        bot_id="b1",
        schedule="every 30m",
        enabled=True,
    )
    registry.register(spec)

    clock = RoutineClock(registry, clock_path=str(tmp_path / "clock.json"), dispatch_fn=AsyncMock())
    # fake last_fired to 2 hours ago
    clock.last_fired["r1"] = datetime(2026, 1, 1, 8, 0, tzinfo=UTC).isoformat()

    with patch("server.bot_team.routine_clock.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        mock_dt.fromisoformat = datetime.fromisoformat
        handoffs = await clock.tick()
        assert len(handoffs) == 1


@pytest.mark.asyncio
async def test_pause_stops_triggers(tmp_path):
    registry = RoutineRegistry(bot_id="b1", storage_path=str(tmp_path / "reg.json"))
    spec = RoutineSpec(
        routine_id="r1",
        trigger_topic="schedule.trigger",
        objective="test",
        goal_template="test",
        bot_id="b1",
        schedule="every 30m",
        enabled=False,
    )
    registry.register(spec)

    clock = RoutineClock(registry, clock_path=str(tmp_path / "clock.json"), dispatch_fn=AsyncMock())
    clock.last_fired["r1"] = datetime(2026, 1, 1, 8, 0, tzinfo=UTC).isoformat()

    with patch("server.bot_team.routine_clock.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        mock_dt.fromisoformat = datetime.fromisoformat
        handoffs = await clock.tick()
        assert len(handoffs) == 0


@pytest.mark.asyncio
async def test_rest_crud(test_app):
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
        # POST
        resp = await ac.post(
            "/api/v1/bots/bot_123/routines/",
            json={"objective": "clean cache", "schedule": "every 1d"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["objective"] == "clean cache"
        rid = data["routine_id"]

        # GET
        resp = await ac.get("/api/v1/bots/bot_123/routines/")
        assert len(resp.json()) == 1

        # PATCH
        resp = await ac.patch(f"/api/v1/bots/bot_123/routines/{rid}", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

        # DELETE
        resp = await ac.delete(f"/api/v1/bots/bot_123/routines/{rid}")
        assert resp.status_code == 200

        # GET again
        resp = await ac.get("/api/v1/bots/bot_123/routines/")
        assert len(resp.json()) == 0


@pytest.mark.asyncio
async def test_run_now_dispatches(test_app, monkeypatch):
    mock_consumer = AsyncMock()
    monkeypatch.setattr("server.routes.routines._default_handoff_consumer", mock_consumer)

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/bots/bot_123/routines/",
            json={"objective": "clean cache", "schedule": "every 1d"},
        )
        rid = resp.json()["routine_id"]

        resp = await ac.post(f"/api/v1/bots/bot_123/routines/{rid}/run_now")
        assert resp.status_code == 200

        mock_consumer.assert_called_once()
        handoff = mock_consumer.call_args[0][0][0]
        assert handoff["routine_id"] == rid
        assert handoff["bot_id"] == "bot_123"
        assert handoff["trigger_topic"] == "schedule.trigger"


@pytest.mark.asyncio
async def test_routines_collection_without_trailing_slash(test_app):
    """The /team UI calls the collection without a trailing slash."""
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/bots/bot_123/routines", json={"objective": "o", "schedule": "every 30m"}
        )
        assert resp.status_code == 200
        resp = await ac.get("/api/v1/bots/bot_123/routines")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1


# ── W11-D (S6): a fired routine runs a bot turn and appends the reply ─────


@pytest.fixture
def fake_master(monkeypatch):
    """Replace the semantic authority with a deterministic reply."""

    from server.coordinator_master import master_coordinator

    async def fake_execute(request):
        from server.goal_run.models import GoalRunResponse, GoalStatus

        return GoalRunResponse(
            goal_id="goal-fake",
            status=GoalStatus.completed,
            phase="finalized",
            reply="现在是 12:03。",
            summary="done",
        )

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)
    yield


@pytest.mark.asyncio
async def test_fired_routine_appends_bot_reply_to_conversation(tmp_path, monkeypatch, fake_master):
    """S6: when a routine is due, the clock fires it and the bot's own
    reply lands in that bot's conversation (not a raw GoalRun repr)."""

    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routines.json"))
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bots.json"))

    from server.bot_team.conversations import ConversationStore
    from server.bot_team.routine_clock import RoutineClock, _default_handoff_consumer
    from server.goal_run.bot_identity import BotRegistry, BotSpec
    from server.goal_run.models import RoutineSpec
    from server.goal_run.routine_registry import RoutineRegistry

    BotRegistry().register(BotSpec(bot_id="bot_123", owner_id="u1", runner_id="local"))

    reg = RoutineRegistry(bot_id="bot_123")
    reg.register(
        RoutineSpec(
            routine_id="r1",
            trigger_topic="schedule.trigger",
            objective="报一次当前时间",
            goal_template="报一次当前时间",
            schedule="every 2m",
            enabled=True,
            bot_id="bot_123",
        )
    )

    clock = RoutineClock(
        reg, clock_path=str(tmp_path / "clock.json"), dispatch_fn=_default_handoff_consumer
    )
    with patch("server.bot_team.routine_clock.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
        mock_dt.fromisoformat = datetime.fromisoformat
        await clock.tick()  # initialise last_fired
        mock_dt.now.return_value = datetime(2026, 1, 1, 11, 3, tzinfo=UTC)
        handoffs = await clock.tick()  # due

    assert len(handoffs) == 1

    store = ConversationStore(root=tmp_path / "bots-root")
    messages = store.list("bot_123", include_hidden=True)
    # A real bot turn records the user message (the objective) and the reply.
    assert any(m["role"] == "user" for m in messages)
    bot_msgs = [m for m in messages if m["role"] == "bot"]
    assert bot_msgs, "the bot's reply was never appended"
    assert bot_msgs[0]["text"] == "现在是 12:03。"
    # The reply carries routine provenance, not a GoalRun repr.
    assert "GoalRunResponse" not in bot_msgs[0]["text"]


@pytest.mark.asyncio
async def test_fired_routine_pause_stops_firing(tmp_path, monkeypatch, fake_master):
    """S6: PATCH enabled=false means the clock no longer fires it."""

    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routines.json"))
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bots.json"))

    from server.bot_team.routine_clock import RoutineClock
    from server.goal_run.bot_identity import BotRegistry, BotSpec
    from server.goal_run.models import RoutineSpec
    from server.goal_run.routine_registry import RoutineRegistry

    BotRegistry().register(BotSpec(bot_id="bot_123", owner_id="u1", runner_id="local"))
    reg = RoutineRegistry(bot_id="bot_123")
    reg.register(
        RoutineSpec(
            routine_id="r1",
            trigger_topic="schedule.trigger",
            objective="o",
            goal_template="o",
            schedule="every 2m",
            enabled=False,
            bot_id="bot_123",
        )
    )
    clock = RoutineClock(reg, clock_path=str(tmp_path / "clock.json"), dispatch_fn=AsyncMock())
    clock.last_fired["r1"] = datetime(2026, 1, 1, 8, 0, tzinfo=UTC).isoformat()
    with patch("server.bot_team.routine_clock.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        mock_dt.fromisoformat = datetime.fromisoformat
        handoffs = await clock.tick()
    assert handoffs == []


@pytest.mark.asyncio
async def test_deleted_routine_no_longer_fires(tmp_path, monkeypatch, fake_master):
    """S6: deleting a routine removes it from the clock's registry."""

    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routines.json"))
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bots.json"))

    from server.bot_team.routine_clock import RoutineClock
    from server.goal_run.bot_identity import BotRegistry, BotSpec
    from server.goal_run.models import RoutineSpec
    from server.goal_run.routine_registry import RoutineRegistry

    BotRegistry().register(BotSpec(bot_id="bot_123", owner_id="u1", runner_id="local"))
    reg = RoutineRegistry(bot_id="bot_123")
    reg.register(
        RoutineSpec(
            routine_id="r1",
            trigger_topic="schedule.trigger",
            objective="o",
            goal_template="o",
            schedule="every 2m",
            enabled=True,
            bot_id="bot_123",
        )
    )
    assert "r1" in reg._items
    del reg._items["r1"]
    reg._save()

    clock = RoutineClock(reg, clock_path=str(tmp_path / "clock.json"), dispatch_fn=AsyncMock())
    clock.last_fired["r1"] = datetime(2026, 1, 1, 8, 0, tzinfo=UTC).isoformat()
    with patch("server.bot_team.routine_clock.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        mock_dt.fromisoformat = datetime.fromisoformat
        handoffs = await clock.tick()
    assert handoffs == []
