"""server.coordinator_master long-task wiring — the actual risk this feature
was built around: does `_default_long_task_factory` genuinely stay a no-op
for every session that never calls goal_start, and does budget enforcement
really work once one does (without the factory ever being told the budget
directly — it must self-heal from the event-sourced projection, since
`_pending_goal_id_ctx` only ever carries a goal_id, never a budget)?
"""

from __future__ import annotations

import contextlib

import pytest
from obase.exceptions import BudgetExceeded

from server import coordinator_master as cm
from server import goal_session_map as gsm
from server.goal_tools import goal_start
from server.tool_registry import _current_master_session


async def _post_round_allow_overspend(driver, cost_usd: float) -> None:
    """QuotaTracker.record_usage writes the quota_paused event to the stream
    *then* raises BudgetExceeded — mirrors how master_agent.py:898-911 catches
    it around post_round in production (the raise is the pause signal, not a
    caller bug)."""
    with contextlib.suppress(BudgetExceeded):
        await driver.post_round({"cost_usd": cost_usd})


@pytest.fixture(autouse=True)
def isolated_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(gsm, "GOAL_LOOPS_DIR", tmp_path / "loops")
    import server.goal_tools as gt

    monkeypatch.setattr(gt, "GOAL_LOOPS_DIR", tmp_path / "loops")


class TestFactoryStaysInertByDefault:
    def test_no_goal_id_in_context_returns_none(self):
        assert cm._pending_goal_id_ctx.get() is None
        assert cm._default_long_task_factory() is None

    def test_random_unassociated_session_returns_none(self):
        token = cm._pending_goal_id_ctx.set(None)
        try:
            assert cm._default_long_task_factory() is None
        finally:
            cm._pending_goal_id_ctx.reset(token)


class TestFactoryConstructsRealDriver:
    @pytest.mark.asyncio
    async def test_factory_returns_driver_bound_to_the_right_goal(self):
        sess_token = _current_master_session.set("wiring-sess-1")
        try:
            await goal_start("test goal", budget_usd=2.0)
            goal_id = gsm.get_goal_id("wiring-sess-1")
        finally:
            _current_master_session.reset(sess_token)

        assert goal_id is not None
        ctx_token = cm._pending_goal_id_ctx.set(goal_id)
        try:
            driver = cm._default_long_task_factory()
        finally:
            cm._pending_goal_id_ctx.reset(ctx_token)

        assert driver is not None
        assert driver.goal_id == goal_id

    @pytest.mark.asyncio
    async def test_budget_self_heals_from_projection_without_factory_passing_it(self):
        """The factory's open_long_task(...) call never passes budget_usd —
        this is the exact thing that would silently zero every goal's budget
        if the self-heal from the event-sourced QuotaView didn't work."""
        sess_token = _current_master_session.set("wiring-sess-2")
        try:
            await goal_start("test goal", budget_usd=7.5)
            goal_id = gsm.get_goal_id("wiring-sess-2")
        finally:
            _current_master_session.reset(sess_token)

        ctx_token = cm._pending_goal_id_ctx.set(goal_id)
        try:
            driver = cm._default_long_task_factory()
            ctx = await driver.pre_round()
        finally:
            cm._pending_goal_id_ctx.reset(ctx_token)

        assert ctx.quota_ok is True
        assert ctx.remaining_usd == pytest.approx(7.5)


class TestBudgetEnforcementActuallyWorks:
    @pytest.mark.asyncio
    async def test_overspend_pauses_next_round_not_before(self):
        sess_token = _current_master_session.set("wiring-sess-3")
        try:
            await goal_start("burn budget", budget_usd=1.0)
            goal_id = gsm.get_goal_id("wiring-sess-3")
        finally:
            _current_master_session.reset(sess_token)

        ctx_token = cm._pending_goal_id_ctx.set(goal_id)
        try:
            driver = cm._default_long_task_factory()
            first = await driver.pre_round()
            assert first.quota_ok is True

            await _post_round_allow_overspend(driver, 1.5)

            second = await driver.pre_round()
            assert second.quota_ok is False
        finally:
            cm._pending_goal_id_ctx.reset(ctx_token)

    @pytest.mark.asyncio
    async def test_a_fresh_driver_instance_sees_the_same_overspend(self):
        """Each factory call builds a brand-new LongTaskDriver (no cached
        singleton) — the enforcement must be readable from the event stream
        by a completely separate driver object, not carried in memory."""
        sess_token = _current_master_session.set("wiring-sess-4")
        try:
            await goal_start("burn budget", budget_usd=1.0)
            goal_id = gsm.get_goal_id("wiring-sess-4")
        finally:
            _current_master_session.reset(sess_token)

        ctx_token = cm._pending_goal_id_ctx.set(goal_id)
        try:
            driver1 = cm._default_long_task_factory()
            await driver1.pre_round()
            await _post_round_allow_overspend(driver1, 2.0)

            driver2 = cm._default_long_task_factory()
            assert driver2 is not driver1
            ctx = await driver2.pre_round()
            assert ctx.quota_ok is False
        finally:
            cm._pending_goal_id_ctx.reset(ctx_token)


class TestSessionsAreIsolated:
    @pytest.mark.asyncio
    async def test_two_sessions_two_independent_goals(self):
        token_a = _current_master_session.set("wiring-sess-a")
        try:
            await goal_start("goal A", budget_usd=1.0)
            goal_a = gsm.get_goal_id("wiring-sess-a")
        finally:
            _current_master_session.reset(token_a)

        token_b = _current_master_session.set("wiring-sess-b")
        try:
            await goal_start("goal B", budget_usd=99.0)
            goal_b = gsm.get_goal_id("wiring-sess-b")
        finally:
            _current_master_session.reset(token_b)

        assert goal_a != goal_b

        ctx_a = cm._pending_goal_id_ctx.set(goal_a)
        try:
            driver_a = cm._default_long_task_factory()
            await driver_a.pre_round()
            await _post_round_allow_overspend(driver_a, 1.5)  # blows session A's $1 budget
        finally:
            cm._pending_goal_id_ctx.reset(ctx_a)

        ctx_b = cm._pending_goal_id_ctx.set(goal_b)
        try:
            driver_b = cm._default_long_task_factory()
            result_b = await driver_b.pre_round()
        finally:
            cm._pending_goal_id_ctx.reset(ctx_b)

        # session B's $99 budget must be completely unaffected by A's overspend
        assert result_b.quota_ok is True
        assert result_b.remaining_usd == pytest.approx(99.0)
