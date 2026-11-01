"""Per-bot turn mutex: one bot, one turn at a time, across conversations."""

from __future__ import annotations

import asyncio

import pytest

from server.bot_team.turn_lock import bot_turn_lock


@pytest.mark.asyncio
async def test_same_bot_turns_serialize():
    order: list[str] = []

    async def turn(name: str) -> None:
        async with bot_turn_lock("beta"):
            order.append(f"{name}-in")
            await asyncio.sleep(0.01)
            order.append(f"{name}-out")

    await asyncio.gather(turn("first"), turn("second"))
    assert order == ["first-in", "first-out", "second-in", "second-out"]


@pytest.mark.asyncio
async def test_same_task_reenters_without_deadlock():
    async with bot_turn_lock("beta"):
        async with bot_turn_lock("beta"):
            async with bot_turn_lock("beta"):
                pass
    # The lock is free again afterwards.
    async with asyncio.timeout(1):
        async with bot_turn_lock("beta"):
            pass


@pytest.mark.asyncio
async def test_different_bots_do_not_block_each_other():
    started: list[str] = []

    async def turn(bot_id: str) -> None:
        async with bot_turn_lock(bot_id):
            started.append(bot_id)
            await asyncio.sleep(0.01)

    await asyncio.gather(turn("alpha"), turn("beta"))
    assert started == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_waiting_turn_releases_on_cancellation():
    acquired = asyncio.Event()

    async def holder() -> None:
        async with bot_turn_lock("beta"):
            acquired.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(holder())
    await acquired.wait()
    waiter = asyncio.create_task(_acquire("beta"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(waiter, timeout=1)


async def _acquire(bot_id: str) -> None:
    async with bot_turn_lock(bot_id):
        pass
