"""AUD2: signal→routine matching contract, as exercised by the two entry points.

What this pins down (verified against the running backend, not assumed):

* A signed per-bot **webhook** (``/api/v1/bots/{id}/routines/webhook``) dispatches a
  routine that declares ``{"kind": "webhook", "event": ...}``.
* ``/api/v1/signals`` deliberately forces ``verified=False`` for ``kind="webhook"``
  (an HTTP caller must not be able to forge a verified webhook), so a webhook
  trigger can never match there — that is fail-closed by design, not a bug.
* A **message** trigger matches only via ``mentions``/``regex``; a trigger that
  declares a bare ``text`` field never matches (the matcher never reads it).
  The UI's test button therefore probes a message trigger, not a webhook one.
"""

from __future__ import annotations

import pytest

from server.goal_run.models import RoutineSpec
from server.goal_run.routine_registry import RoutineRegistry
from server.nano_w9.routine_triggers import RoutineSignalRouter


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routines.json"))
    monkeypatch.setenv("VEYA_ROUTINE_HISTORY_DIR", str(tmp_path / "history"))
    monkeypatch.setenv("VEYA_EVENT_TRIGGERS", "1")
    monkeypatch.setenv("VEYA_ROUTINE_TRIGGERS", "1")
    return tmp_path


def _routine(bot_id: str, triggers: list[dict]) -> RoutineSpec:
    return RoutineSpec(
        routine_id="r-1",
        trigger_topic="event.arrive",
        objective="摘要",
        goal_template="摘要",
        enabled=True,
        bot_id=bot_id,
        triggers=triggers,
    )


def _router(bot_id: str) -> RoutineSignalRouter:
    return RoutineSignalRouter(
        registry=RoutineRegistry(bot_id=bot_id), dispatch=lambda **kw: {"handoffs": []}
    )


@pytest.mark.asyncio
async def test_verified_webhook_signal_matches_webhook_trigger(env):
    """The signed-webhook path matches (this is what the webhook route produces)."""
    RoutineRegistry(bot_id="b1").register(_routine("b1", [{"kind": "webhook", "event": "deploy"}]))

    result = await _router("b1").handle(
        {"kind": "webhook", "event": "deploy", "verified": True, "id": "s1"}
    )

    assert result["status"] == "ok"
    assert [f["routine_id"] for f in result["fired"]] == ["r-1"]


@pytest.mark.asyncio
async def test_unverified_webhook_signal_never_matches(env):
    """/api/v1/signals forces verified=False, so it must NOT match — fail-closed."""
    RoutineRegistry(bot_id="b1").register(_routine("b1", [{"kind": "webhook", "event": "deploy"}]))

    result = await _router("b1").handle(
        {"kind": "webhook", "event": "deploy", "verified": False, "id": "s2"}
    )

    assert result["status"] == "no_match"


@pytest.mark.asyncio
async def test_message_trigger_matches_on_mentions(env):
    """A message trigger with `mentions` matches — this is what the UI test button probes."""
    RoutineRegistry(bot_id="b1").register(_routine("b1", [{"kind": "message", "mentions": ["发布"]}]))

    result = await _router("b1").handle(
        {"kind": "message", "text": "[UI 测试] 发布", "verified": False, "id": "s3"}
    )

    assert result["status"] == "ok"
    assert [f["routine_id"] for f in result["fired"]] == ["r-1"]


@pytest.mark.asyncio
async def test_message_trigger_with_bare_text_never_matches(env):
    """Documents why the UI must create `mentions` triggers, not `text` triggers."""
    RoutineRegistry(bot_id="b1").register(_routine("b1", [{"kind": "message", "text": "发布"}]))

    result = await _router("b1").handle(
        {"kind": "message", "text": "[UI 测试] 发布", "verified": False, "id": "s4"}
    )

    assert result["status"] == "no_match"
