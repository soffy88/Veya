"""Hark parity Phase 0: in-process live checks for scheduler and approvals.

These run the real RoutineClock, the real AutomataScheduler and the real
ApprovalService against temp paths. Nothing here touches production state and
nothing is dispatched to a live agent: the dispatch/execute callbacks are
in-process fakes, so a failing assertion is a real bug in the scheduling or
approval logic rather than an environment problem.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from server.bot_team.routine_clock import RoutineClock, compute_due
from server.goal_run.models import RoutineSpec
from server.goal_run.routine_registry import RoutineRegistry


def _spec(
    routine_id: str = "r1",
    *,
    schedule: str = "* * * * *",
    enabled: bool = True,
    probe: dict[str, Any] | None = None,
    workspace: str = "",
) -> RoutineSpec:
    return RoutineSpec(
        routine_id=routine_id,
        trigger_topic="schedule.trigger",
        objective="objective",
        bot_id="parity-bot",
        schedule=schedule,
        goal_template="goal",
        enabled=enabled,
        probe=probe or {"kind": "none"},
        workspace_path=workspace,
    )


@pytest.fixture
def registry(tmp_path: Path) -> RoutineRegistry:
    return RoutineRegistry(storage_path=tmp_path / "routines.json", bot_id="parity-bot")


# ── compute_due: the schedule arithmetic behind the clock ────────────────


def test_interval_schedule_reports_most_recent_missed_occurrence() -> None:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    due = compute_due(
        _spec(schedule="every 1h"), now=now, last_fired_utc=now - timedelta(hours=3, minutes=10)
    )
    # 190 minutes elapsed -> the most recent 60-minute slot is 11:50, not 11:00.
    assert due == "2026-10-21T11:50:00+00:00"


def test_interval_schedule_not_due_before_next_slot() -> None:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    assert (
        compute_due(_spec(schedule="every 1h"), now=now, last_fired_utc=now - timedelta(minutes=20))
        is None
    )


def test_daily_schedule_is_timezone_aware() -> None:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    spec = RoutineSpec(
        routine_id="r1",
        trigger_topic="schedule.trigger",
        objective="o",
        goal_template="g",
        schedule="daily 09:00",
        timezone="Asia/Shanghai",
    )
    due = compute_due(spec, now=now, last_fired_utc=now - timedelta(days=1))
    # 09:00 Shanghai == 01:00 UTC on the same day.
    assert due == "2026-10-21T01:00:00+00:00"


def test_cron_weekday_window() -> None:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)  # Wednesday
    spec = RoutineSpec(
        routine_id="r1",
        trigger_topic="schedule.trigger",
        objective="o",
        goal_template="g",
        schedule="0 9 * * 1-5",
        timezone="UTC",
    )
    due = compute_due(spec, now=now, last_fired_utc=now - timedelta(days=1))
    assert due == "2026-10-21T09:00:00+00:00"


def test_empty_schedule_never_fires() -> None:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    assert compute_due(_spec(schedule=""), now=now, last_fired_utc=now - timedelta(days=1)) is None


# ── RoutineClock.tick: dispatch and state ───────────────────────────────


@pytest.mark.asyncio
async def test_first_tick_baselines_without_dispatching(registry, tmp_path) -> None:
    registry.register(_spec())
    fired: list[dict] = []

    async def dispatch(handoffs):
        fired.extend(handoffs)

    clock = RoutineClock(registry, clock_path=tmp_path / "clock.json", dispatch_fn=dispatch)
    assert await clock.tick() == []
    assert "r1" in clock.last_fired
    assert fired == []


@pytest.mark.asyncio
async def test_due_routine_dispatches_once_and_persists_state(registry, tmp_path) -> None:
    registry.register(_spec())
    fired: list[dict] = []

    async def dispatch(handoffs):
        fired.extend(handoffs)

    clock_path = tmp_path / "clock.json"
    clock = RoutineClock(registry, clock_path=clock_path, dispatch_fn=dispatch)
    clock.last_fired["r1"] = "2020-01-01T00:00:00+00:00"

    handoffs = await clock.tick()
    assert len(handoffs) == 1
    assert handoffs[0]["routine_id"] == "r1"
    assert handoffs[0]["target"] == {"goal_template": "goal"}
    assert handoffs[0]["trigger_topic"] == "schedule.trigger"
    assert fired == handoffs
    assert clock_path.exists()

    # The clock advanced past `now`, so an immediate second tick is quiet.
    assert await clock.tick() == []


@pytest.mark.asyncio
async def test_disabled_routine_never_dispatches(registry, tmp_path) -> None:
    registry.register(_spec(enabled=False))
    fired: list[dict] = []

    async def dispatch(handoffs):
        fired.extend(handoffs)

    clock = RoutineClock(registry, clock_path=tmp_path / "clock.json", dispatch_fn=dispatch)
    clock.last_fired["r1"] = "2020-01-01T00:00:00+00:00"
    assert await clock.tick() == []
    assert fired == []


@pytest.mark.asyncio
async def test_failed_probe_skips_dispatch(registry, tmp_path) -> None:
    registry.register(_spec(probe={"kind": "git_changes", "path": str(tmp_path / "not-a-repo")}))
    fired: list[dict] = []

    async def dispatch(handoffs):
        fired.extend(handoffs)

    clock = RoutineClock(registry, clock_path=tmp_path / "clock.json", dispatch_fn=dispatch)
    clock.last_fired["r1"] = "2020-01-01T00:00:00+00:00"
    assert await clock.tick() == []
    assert fired == []


@pytest.mark.asyncio
async def test_git_probe_without_changes_skips_dispatch(registry, tmp_path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    registry.register(_spec(probe={"kind": "git_changes", "path": str(repo)}))
    fired: list[dict] = []

    async def dispatch(handoffs):
        fired.extend(handoffs)

    clock = RoutineClock(registry, clock_path=tmp_path / "clock.json", dispatch_fn=dispatch)
    clock.last_fired["r1"] = "2020-01-01T00:00:00+00:00"
    assert await clock.tick() == []
    assert fired == []


@pytest.mark.asyncio
async def test_dispatch_failure_does_not_break_the_clock(registry, tmp_path) -> None:
    registry.register(_spec())

    async def dispatch(_handoffs):
        raise RuntimeError("consumer down")

    clock = RoutineClock(registry, clock_path=tmp_path / "clock.json", dispatch_fn=dispatch)
    clock.last_fired["r1"] = "2020-01-01T00:00:00+00:00"
    handoffs = await clock.tick()
    assert len(handoffs) == 1  # the tick still reports what it tried


@pytest.mark.asyncio
async def test_clock_state_survives_restart(registry, tmp_path) -> None:
    registry.register(_spec())
    clock_path = tmp_path / "clock.json"

    async def dispatch(_handoffs):
        return None

    first = RoutineClock(registry, clock_path=clock_path, dispatch_fn=dispatch)
    first.last_fired["r1"] = "2020-01-01T00:00:00+00:00"
    await first.tick()

    second = RoutineClock(registry, clock_path=clock_path, dispatch_fn=dispatch)
    assert second.last_fired["r1"] == first.last_fired["r1"]


# ── Automata cron scheduler (real APScheduler, temp jobs db) ────────────


@pytest.mark.asyncio
async def test_automata_register_list_remove_persist(tmp_path) -> None:
    from server.automata import VeyaAutomata

    executed: list[tuple[str, str]] = []

    async def callback(prompt: str, user_id: str = "") -> str:
        executed.append((prompt, user_id))
        return "done"

    jobs_db = tmp_path / "jobs.json"
    automata = VeyaAutomata(callback, jobs_db_path=jobs_db, restore_on_start=False)
    try:
        message = automata.register_cron_task("0 9 * * *", "morning brief", task_id="cron_p1")
        assert "cron_p1" in message
        assert [job["id"] for job in automata.get_jobs()] == ["cron_p1"]
        assert automata.get_jobs()[0]["next_run"]

        persisted = jobs_db.read_text()
        assert "morning brief" in persisted and "0 9 * * *" in persisted

        assert automata.remove_task("cron_p1").startswith("✅")
        assert automata.get_jobs() == []
    finally:
        automata.shutdown()


@pytest.mark.asyncio
async def test_automata_rejects_bad_cron_and_empty_prompt(tmp_path) -> None:
    from server.automata import VeyaAutomata

    async def callback(_prompt: str) -> str:
        return "done"

    automata = VeyaAutomata(callback, jobs_db_path=tmp_path / "jobs.json", restore_on_start=False)
    try:
        with pytest.raises(ValueError):
            automata.register_cron_task("not a cron", "x")
        with pytest.raises(ValueError):
            automata.register_cron_task("0 9 * * *", "   ")
    finally:
        automata.shutdown()


@pytest.mark.asyncio
async def test_automata_restores_persisted_jobs(tmp_path) -> None:
    from server.automata import VeyaAutomata

    async def callback(_prompt: str) -> str:
        return "done"

    jobs_db = tmp_path / "jobs.json"
    first = VeyaAutomata(callback, jobs_db_path=jobs_db, restore_on_start=False)
    first.register_cron_task("0 9 * * *", "restored brief", task_id="cron_restore")
    first.shutdown()

    second = VeyaAutomata(callback, jobs_db_path=jobs_db, restore_on_start=True)
    try:
        assert [job["id"] for job in second.get_jobs()] == ["cron_restore"]
    finally:
        second.shutdown()


@pytest.mark.asyncio
async def test_automata_event_trigger_runs_headless_callback(tmp_path) -> None:
    from server.automata import VeyaAutomata

    executed: list[str] = []

    async def callback(prompt: str) -> str:
        executed.append(prompt)
        return "ok"

    automata = VeyaAutomata(callback, jobs_db_path=tmp_path / "jobs.json", restore_on_start=False)
    try:
        assert automata.trigger_event("webhook", {"id": 7}).startswith("✅")
        for _ in range(50):
            if executed:
                break
            await asyncio.sleep(0.02)
        assert executed and "EVENT TRIGGER: webhook" in executed[0]
    finally:
        automata.shutdown()


# ── approvals: request → resolve → in-process wake ──────────────────────


@pytest.fixture
def approval_service(tmp_path, monkeypatch):
    from server import approvals as approvals_mod
    from veya.obase.approvals import ApprovalStore

    monkeypatch.setenv("VEYA_APPROVAL_GATES", "1")
    approvals_mod.configure_service(store=ApprovalStore(str(tmp_path / "approvals.db")))
    approvals_mod.session_grants.clear()
    approvals_mod._pending_waiters.clear()
    yield approvals_mod.get_approval_service()
    approvals_mod.session_grants.clear()
    approvals_mod._pending_waiters.clear()


@pytest.mark.asyncio
async def test_approval_request_and_human_resolve(approval_service) -> None:
    service = approval_service
    record = await service.request(
        tool="send_email", args={"to": "a@b"}, user_id="user-1", session_id="s1"
    )
    assert record["status"] == "completed"
    scope = service.scope_for("user-1")
    pending = service.list_pending(scope=scope)
    assert [row["tool"] for row in pending] == ["send_email"]

    resolved = await service.resolve(
        approval_id=record["approval_id"], decision="once", principal="human"
    )
    assert resolved["approval"]["status"] == "resolved"
    assert service.list_pending(scope=scope) == []


@pytest.mark.asyncio
async def test_high_risk_approval_rejects_agent_principal(approval_service) -> None:
    from fastapi import HTTPException

    service = approval_service
    record = await service.request(tool="payment_charge", args={}, user_id="user-1")
    with pytest.raises(HTTPException) as excinfo:
        await service.resolve(
            approval_id=record["approval_id"], decision="once", principal="agent:master"
        )
    assert excinfo.value.status_code == 403
    assert service.get(record["approval_id"])["status"] == "pending"


@pytest.mark.asyncio
async def test_double_resolve_is_rejected(approval_service) -> None:
    from fastapi import HTTPException

    service = approval_service
    record = await service.request(tool="send_email", args={}, user_id="user-1")
    await service.resolve(approval_id=record["approval_id"], decision="once", principal="human")
    with pytest.raises(HTTPException) as excinfo:
        await service.resolve(approval_id=record["approval_id"], decision="once", principal="human")
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_agent_wait_loop_resumes_on_human_resolution(approval_service) -> None:
    """The gated wait path wakes when the human resolves through the API service."""

    from server import approvals as approvals_mod
    from server import auth as auth_mod
    from server import user_control as uc

    user_token = auth_mod._user_ctx.set({"user_id": "user-1", "username": "user-1"})
    sid_token = uc._session_id.set("s1")
    try:
        waiter = asyncio.create_task(uc._wait_approval("send_email", {"to": "a@b"}))
        for _ in range(100):
            if approvals_mod._pending_waiters:
                break
            await asyncio.sleep(0.02)
        request_id = next(iter(approvals_mod._pending_waiters))
        await approval_service.resolve(approval_id=request_id, decision="once", principal="human")
        assert await asyncio.wait_for(waiter, 10) is None
    finally:
        uc._session_id.reset(sid_token)
        auth_mod._user_ctx.reset(user_token)


@pytest.mark.asyncio
async def test_agent_wait_loop_returns_reason_on_deny(approval_service) -> None:
    from server import approvals as approvals_mod
    from server import auth as auth_mod
    from server import user_control as uc

    user_token = auth_mod._user_ctx.set({"user_id": "user-1", "username": "user-1"})
    sid_token = uc._session_id.set("s1")
    try:
        waiter = asyncio.create_task(uc._wait_approval("send_email", {"to": "a@b"}))
        for _ in range(100):
            if approvals_mod._pending_waiters:
                break
            await asyncio.sleep(0.02)
        request_id = next(iter(approvals_mod._pending_waiters))
        await approval_service.resolve(approval_id=request_id, decision="deny", principal="human")
        reason = await asyncio.wait_for(waiter, 10)
        assert reason and "denied" in reason.lower()
    finally:
        uc._session_id.reset(sid_token)
        auth_mod._user_ctx.reset(user_token)


@pytest.mark.asyncio
async def test_handoff_request_and_resume(approval_service) -> None:
    service = approval_service
    created = await service.request_handoff(reason="user must confirm 2FA", user_id="user-1")
    handoff_id = created["handoff"]["id"]
    assert service.get_handoff(handoff_id)["status"] == "pending"
    resumed = await service.resume_handoff(handoff_id=handoff_id, done=True)
    assert resumed["status"] == "completed"
    assert service.get_handoff(handoff_id)["status"] != "pending"


def test_ask_once_cache_never_caches_destructive_actions(monkeypatch) -> None:
    from server.ask_once_cache import AskOnceCache

    monkeypatch.setenv("VEYA_APPROVAL_ASK_ONCE", "1")
    cache = AskOnceCache()
    assert cache.record("s1", "delete_file", {"path": "/x"}) is False
    assert cache.check("s1", "delete_file", {"path": "/x"}) is None

    assert cache.record("s1", "browser_click", {"ref": "e1"}) is True
    assert cache.check("s1", "browser_click", {"ref": "e1"}) is not None
    assert cache.check("s1", "browser_click", {"ref": "e2"}) is None
