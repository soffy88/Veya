"""The engine's own `master_done` must not truncate the answer.

Regression for the production symptom (2026-10-23): a browser task streamed
master_start / master_round / tool_call / master_done{round:N} and no
text_delta, so the client rendered an empty answer.

`MasterAgent.chat_stream` notifies `master_done` when its ReAct loop decides
the turn is over; that event carries only the round count. The final answer is
returned to `server.chat_stream._finish`, which publishes `text_delta(final)`
and then the terminal event. Publishing the engine's early event as a durable
terminal ended the stream before the answer existed.
"""

from __future__ import annotations

import json
import uuid

import pytest

from server.chat_stream import new_agent_stream_events
from server.coordinator_master import MasterCoordinator
from server.events import fire_step


def _data_frames(frames: list[str]) -> list[dict]:
    out = []
    for frame in frames:
        for line in frame.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                out.append(json.loads(line[len("data: ") :]))
    return out


@pytest.mark.asyncio
async def test_engine_master_done_does_not_truncate_final_answer(monkeypatch):
    async def fake_chat_stream(self, text, *, session_id=None, **_kwargs):
        # What the ReAct loop does: tool progress, then an early terminal event.
        fire_step({"type": "master_start", "session_id": session_id})
        fire_step({"type": "master_round", "session_id": session_id, "round": 1})
        fire_step(
            {
                "type": "tool_call",
                "session_id": session_id,
                "tool_name": "browser_run",
                "round": 1,
            }
        )
        fire_step({"type": "master_done", "session_id": session_id, "round": 1})
        return {
            "status": "success",
            "final_answer": "页面标题是 Example Domain",
            "rounds": 1,
            "cost_usd": 0.0,
        }

    monkeypatch.setattr(MasterCoordinator, "chat_stream", fake_chat_stream)

    sid = f"sid-terminal-{uuid.uuid4().hex[:8]}"
    frames = [frame async for frame in new_agent_stream_events("标题", session_id=sid)]
    events = _data_frames(frames)

    kinds = [e.get("type") for e in events]
    assert kinds[-1] == "master_done", kinds
    assert kinds.count("master_done") == 1, "exactly one terminal event per turn"

    deltas = "".join(str(e.get("delta") or "") for e in events if e.get("type") == "text_delta")
    assert "Example Domain" in deltas

    # The answer must precede the terminal event, otherwise a client that stops
    # at master_done never renders it.
    text_index = next(
        i
        for i, e in enumerate(events)
        if e.get("type") == "text_delta" and "Example Domain" in str(e.get("delta") or "")
    )
    assert text_index < kinds.index("master_done")


@pytest.mark.asyncio
async def test_replay_after_completion_includes_the_final_answer(monkeypatch):
    """GET /stream/{sid} replay must contain text_delta(final) too."""

    async def fake_chat_stream(self, text, *, session_id=None, **_kwargs):
        fire_step({"type": "master_done", "session_id": session_id, "round": 1})
        return {"status": "success", "final_answer": "回放答案", "rounds": 1}

    monkeypatch.setattr(MasterCoordinator, "chat_stream", fake_chat_stream)

    sid = f"sid-replay-{uuid.uuid4().hex[:8]}"
    async for _frame in new_agent_stream_events("q", session_id=sid):
        pass

    # The completed turn is terminal, so this second call takes the replay path.
    frames = [frame async for frame in new_agent_stream_events("q", session_id=sid)]
    events = _data_frames(frames)
    deltas = "".join(str(e.get("delta") or "") for e in events if e.get("type") == "text_delta")
    assert "回放答案" in deltas
