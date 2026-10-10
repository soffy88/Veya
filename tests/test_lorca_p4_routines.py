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
    app.include_router(router)
    return app

def test_cron_due_computation():
    now = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
    last_fired = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    spec = RoutineSpec(routine_id="r1", trigger_topic="schedule.trigger", objective="test", goal_template="test", bot_id="b1", schedule="0 9 * * *", timezone="UTC")
    due = compute_due(spec, now=now, last_fired_utc=last_fired)
    assert due == "2026-01-01T09:00:00+00:00"

def test_interval_parsing():
    now = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    last_fired = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    spec = RoutineSpec(routine_id="r1", trigger_topic="schedule.trigger", objective="test", goal_template="test", bot_id="b1", schedule="every 30m")
    due = compute_due(spec, now=now, last_fired_utc=last_fired)
    # The catch-up gives most recent past occurrence
    assert due == "2026-01-01T10:00:00+00:00"

def test_daily_time_parsing():
    now = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    last_fired = datetime(2025, 12, 31, 8, 0, tzinfo=UTC)
    spec = RoutineSpec(routine_id="r1", trigger_topic="schedule.trigger", objective="test", goal_template="test", bot_id="b1", schedule="daily 09:00", timezone="UTC")
    due = compute_due(spec, now=now, last_fired_utc=last_fired)
    assert due == "2026-01-01T09:00:00+00:00"

def test_timezone_support():
    now = datetime(2026, 1, 1, 10, 0, tzinfo=UTC) # 18:00 Asia/Shanghai
    last_fired = datetime(2025, 12, 31, 8, 0, tzinfo=UTC)
    # 09:00 in Asia/Shanghai is 01:00 UTC
    spec = RoutineSpec(routine_id="r1", trigger_topic="schedule.trigger", objective="test", goal_template="test", bot_id="b1", schedule="daily 09:00", timezone="Asia/Shanghai")
    due = compute_due(spec, now=now, last_fired_utc=last_fired)
    assert due == "2026-01-01T01:00:00+00:00"

@pytest.mark.asyncio
async def test_no_double_fire_across_restart(tmp_path):
    registry = RoutineRegistry(bot_id="b1", storage_path=str(tmp_path/"reg.json"))
    spec = RoutineSpec(routine_id="r1", trigger_topic="schedule.trigger", objective="test", goal_template="test", bot_id="b1", schedule="every 30m", enabled=True)
    registry.register(spec)

    clock = RoutineClock(registry, clock_path=str(tmp_path/"clock.json"), dispatch_fn=AsyncMock())

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
    registry = RoutineRegistry(bot_id="b1", storage_path=str(tmp_path/"reg.json"))
    spec = RoutineSpec(routine_id="r1", trigger_topic="schedule.trigger", objective="test", goal_template="test", bot_id="b1", schedule="every 30m", enabled=True)
    registry.register(spec)

    clock = RoutineClock(registry, clock_path=str(tmp_path/"clock.json"), dispatch_fn=AsyncMock())
    # fake last_fired to 2 hours ago
    clock.last_fired["r1"] = datetime(2026, 1, 1, 8, 0, tzinfo=UTC).isoformat()

    with patch("server.bot_team.routine_clock.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        mock_dt.fromisoformat = datetime.fromisoformat
        handoffs = await clock.tick()
        assert len(handoffs) == 1

@pytest.mark.asyncio
async def test_pause_stops_triggers(tmp_path):
    registry = RoutineRegistry(bot_id="b1", storage_path=str(tmp_path/"reg.json"))
    spec = RoutineSpec(routine_id="r1", trigger_topic="schedule.trigger", objective="test", goal_template="test", bot_id="b1", schedule="every 30m", enabled=False)
    registry.register(spec)

    clock = RoutineClock(registry, clock_path=str(tmp_path/"clock.json"), dispatch_fn=AsyncMock())
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
        resp = await ac.post("/api/v1/bots/bot_123/routines/", json={
            "objective": "clean cache",
            "schedule": "every 1d"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["objective"] == "clean cache"
        rid = data["routine_id"]

        # GET
        resp = await ac.get("/api/v1/bots/bot_123/routines/")
        assert len(resp.json()) == 1

        # PATCH
        resp = await ac.patch(f"/api/v1/bots/bot_123/routines/{rid}", json={
            "enabled": False
        })
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
        resp = await ac.post("/api/v1/bots/bot_123/routines/", json={
            "objective": "clean cache",
            "schedule": "every 1d"
        })
        rid = resp.json()["routine_id"]

        resp = await ac.post(f"/api/v1/bots/bot_123/routines/{rid}/run_now")
        assert resp.status_code == 200

        mock_consumer.assert_called_once()
        handoff = mock_consumer.call_args[0][0][0]
        assert handoff["routine_id"] == rid
        assert handoff["bot_id"] == "bot_123"
        assert handoff["trigger_topic"] == "schedule.trigger"
