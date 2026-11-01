"""Repeat guard: identical tool calls in one turn are steered, never blocked."""

from __future__ import annotations

import pytest

from server.bot_team.repeat_guard import (
    STEER_AT,
    RepeatDetector,
    attach_steer,
    bind_repeat_detector,
    current_repeat_detector,
    reset_repeat_detector,
    steer_note,
)
from server.tool_registry import MasterToolRegistry


def test_detector_counts_identical_calls_only():
    detector = RepeatDetector()
    assert detector.observe("read", {"path": "a", "n": 1}) == 1
    assert detector.observe("read", {"n": 1, "path": "a"}) == 2
    assert detector.observe("read", {"path": "b", "n": 1}) == 1
    assert detector.observe("write", {"path": "a", "n": 1}) == 1
    assert detector.observe("read", {}) == 0
    assert detector.count("read", {"path": "a", "n": 1}) == 2
    assert detector.count("read", {}) == 0


def test_steer_note_threshold():
    assert steer_note("read", STEER_AT - 1) is None
    note = steer_note("read", STEER_AT)
    assert note is not None and "read" in note and str(STEER_AT) in note
    assert attach_steer("ok", "read", STEER_AT - 1) == "ok"
    assert attach_steer("ok", "read", STEER_AT).startswith("ok\n[repeat guard:")


def _registry() -> MasterToolRegistry:
    registry = MasterToolRegistry()

    def echo(q: str) -> str:
        return f"got:{q}"

    registry.register(
        "echo",
        "Echo the query back.",
        {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
            "additionalProperties": False,
        },
        echo,
    )

    def ping() -> str:
        return "pong"

    registry.register(
        "ping",
        "Answer pong; takes no arguments.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        ping,
    )
    return registry


@pytest.mark.asyncio
async def test_registry_steers_fifth_identical_call():
    registry = _registry()
    token = bind_repeat_detector()
    try:
        results = [await registry.execute("echo", {"q": "same"}) for _ in range(5)]
    finally:
        reset_repeat_detector(token)
    assert all("[repeat guard:" not in result for result in results[:4])
    assert results[4].startswith("got:same\n[repeat guard:")
    assert "5th identical call" in results[4]


@pytest.mark.asyncio
async def test_registry_ignores_varied_calls_and_unbound_turns():
    registry = _registry()
    token = bind_repeat_detector()
    try:
        for i in range(5):
            result = await registry.execute("echo", {"q": f"q{i}"})
            assert "[repeat guard:" not in result
        for _ in range(5):
            result = await registry.execute("ping", {})
            assert "[repeat guard:" not in result
    finally:
        reset_repeat_detector(token)
    assert current_repeat_detector() is None
    for _ in range(5):
        result = await registry.execute("echo", {"q": "same"})
        assert "[repeat guard:" not in result


def test_nested_binding_gets_fresh_detector():
    outer = bind_repeat_detector()
    try:
        detector = current_repeat_detector()
        assert detector is not None
        for _ in range(4):
            detector.observe("read", {"path": "a"})
        inner = bind_repeat_detector()
        try:
            fresh = current_repeat_detector()
            assert fresh is not None and fresh is not detector
            assert fresh.observe("read", {"path": "a"}) == 1
        finally:
            reset_repeat_detector(inner)
        assert current_repeat_detector() is detector
        assert detector.observe("read", {"path": "a"}) == 5
    finally:
        reset_repeat_detector(outer)
    assert current_repeat_detector() is None
