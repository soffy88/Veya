"""Release-startup guards for optional integrations."""

import asyncio

import pytest

from server.app import _bounded_optional_start


@pytest.mark.asyncio
async def test_optional_integration_start_is_bounded() -> None:
    async def slow_start() -> object:
        await asyncio.sleep(1)
        return None

    with pytest.raises(asyncio.TimeoutError):
        await _bounded_optional_start(slow_start, timeout_s=0.01)
