"""NANO-W10 tests: always-on agent ergonomics.

One file for the whole wave, per `NANO_W10_SPEC.md` line 2.  Sections are added
in spec order (1..7, then the ADDENDUMs).  Every dependency that would touch
``~/.veya`` or a live service is injected; temp dirs are the only filesystem.

Run with:  .venv/bin/python -m pytest -q tests/test_nano_w10_always_on.py
"""

from __future__ import annotations

import json

import pytest

from server.nano_w10.flags import ALL_FLAGS, FeatureDisabled, feature_flags, flag_enabled, require_flag
from server.nano_w10.turn_queue import TurnQueue
from veya.omodul.turn_intake import (
    TurnIntakeConfig,
    TurnIntakeInput,
    compute_fingerprint_for,
    turn_intake,
)
from veya.oprim.turn_envelope import envelope_fingerprint, idem_key
from veya.oskill.pure.turn_priority import (
    INBOUND_KINDS,
    STATE_DELIVERED,
    STATE_UNCONFIRMED,
    classify_inbound,
    coalesce_window,
    envelope_priority,
    order_queue,
    steer_decision,
)


# ══════════════════════════════════════════════════════════════════════════
# 0 — flags (HARD RULES: default OFF except 7a / read-only surfaces)
# ══════════════════════════════════════════════════════════════════════════


def test_flags_default_off_except_readonly_surfaces() -> None:
    on_by_default = {flag for flag, default in ALL_FLAGS.items() if default}
    assert on_by_default == {
        "VEYA_TOOL_ERROR_FRIENDLY",
        "VEYA_LLM_TAP",
        "VEYA_SEARCH_FALLBACK",
        "VEYA_CHANNEL_STATUS",
    }
    for flag in ("VEYA_TURN_QUEUE", "VEYA_AUTO_REVIEW", "VEYA_PROMPT_CACHE", "VEYA_LOOP_MODE"):
        assert ALL_FLAGS[flag] is False
        assert flag_enabled(flag) is False


def test_require_flag_reports_which_flag_is_off() -> None:
    with pytest.raises(FeatureDisabled) as excinfo:
        require_flag("VEYA_TURN_QUEUE")
    assert excinfo.value.as_error() == {
        "code": "FEATURE_DISABLED",
        "flag": "VEYA_TURN_QUEUE",
    }
    assert set(feature_flags()) == set(ALL_FLAGS)


# ══════════════════════════════════════════════════════════════════════════
# 1 — turn queue: priority + coalescing + steer + idempotency
# ══════════════════════════════════════════════════════════════════════════


def test_priority_order_is_the_spec_order() -> None:
    assert INBOUND_KINDS == ("direct", "group_mention", "channel", "routine")
    assert [envelope_priority({"kind": k}) for k in INBOUND_KINDS] == [0, 1, 2, 3]


def test_unknown_kind_never_outranks_a_user_message() -> None:
    assert classify_inbound({"kind": "who-knows"}) == "routine"
    assert classify_inbound({}) == "routine"
    assert classify_inbound({"user_directed": True}) == "direct"
    assert classify_inbound({"mention": True}) == "group_mention"


def test_order_queue_is_stable_on_arrival_ties() -> None:
    queued = [
        {"kind": "routine", "arrived_at": 10.0, "text": "r1"},
        {"kind": "channel", "arrived_at": 10.0, "text": "c1"},
        {"kind": "direct", "arrived_at": 11.0, "text": "d1"},
        {"kind": "group_mention", "arrived_at": 9.0, "text": "g1"},
    ]
    # Priority first (direct beats a mention even though it arrived later),
    # then arrival order so equal-priority traffic never reorders.
    assert [item["text"] for item in order_queue(queued)] == ["d1", "g1", "c1", "r1"]


def test_direct_message_steers_a_running_turn() -> None:
    decision = steer_decision({"kind": "direct"}, turn_state="running", has_start_evidence=True)
    assert decision["action"] == "steer"
    # W6 K receipt vocabulary is reused verbatim — no second state machine.
    assert decision["state"] in {"delivered", "started", "unconfirmed"}


def test_non_direct_traffic_queues_behind_a_running_turn() -> None:
    for kind in ("group_mention", "channel", "routine"):
        assert steer_decision({"kind": kind}, turn_state="running")["action"] == "queue"


def test_coalesce_merges_inside_the_window_and_keeps_highest_priority() -> None:
    members = [
        {"kind": "routine", "text": "nightly digest", "arrived_at": 100.0},
        {"kind": "direct", "text": "deploy now", "arrived_at": 100.4},
    ]
    merged = coalesce_window(members, now=100.9, window_s=2.0)
    assert merged["coalesced"] is True
    assert merged["count"] == 2
    assert merged["kind"] == "direct"
    assert merged["text"].splitlines() == ["deploy now", "nightly digest"]


def test_coalesce_window_is_configurable_and_zero_means_no_merge() -> None:
    members = [{"kind": "direct", "text": "a", "arrived_at": 100.0}]
    # 1.0s after the first message: inside the 2s window, outside a 0.5s one.
    assert coalesce_window(members, now=101.0, window_s=2.0)["coalesced"] is True
    assert coalesce_window(members, now=101.0, window_s=0.5)["coalesced"] is False
    assert coalesce_window(members, now=101.0, window_s=0.0)["coalesced"] is False
    assert coalesce_window([], now=1.0)["count"] == 0


def test_idempotency_key_is_stable_per_message_and_differs_across_messages() -> None:
    assert idem_key("c1", "user", "m1") == idem_key("c1", "user", "m1")
    assert idem_key("c1", "user", "m1") != idem_key("c1", "user", "m2")
    assert idem_key("c1", "user", "m1") != idem_key("c2", "user", "m1")
    assert envelope_fingerprint({"a": 1}, ()) == ""
    assert envelope_fingerprint({"a": 1}, ("a",)) == envelope_fingerprint({"a": 1}, ("a",))


def test_turn_intake_is_a_pure_function_of_its_collaborators() -> None:
    seen: set[str] = set()
    pending: list[dict[str, object]] = []
    config = TurnIntakeConfig(
        turn_state=lambda: "none",
        list_pending=lambda: list(pending),
        append_pending=lambda envelope, text: pending.append(dict(envelope)),
        seen_before=lambda key: key in seen,
        mark_seen=lambda key, envelope: seen.add(key),
        coalesce_window_s=2.0,
    )
    first = turn_intake(
        config,
        TurnIntakeInput(conversation_ref="c1", kind="direct", text="hi", external_id="m1", arrived_at=1.0),
        None,
        now=1.0,
    )
    assert first["status"] == "completed"
    assert first["action"] == "queue"
    retry = turn_intake(
        config,
        TurnIntakeInput(conversation_ref="c1", kind="direct", text="hi", external_id="m1", arrived_at=1.2),
        None,
        now=1.2,
    )
    assert retry["action"] == "drop"
    assert retry["idem_key"] == first["idem_key"]
    assert len(pending) == 1


def test_turn_intake_never_raises_on_a_missing_conversation_ref() -> None:
    result = turn_intake(TurnIntakeConfig(), TurnIntakeInput(conversation_ref=""), None, now=1.0)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["action"] == "drop"
    # The fingerprint helper degrades to "" rather than exploding.
    assert compute_fingerprint_for(TurnIntakeConfig(), TurnIntakeInput(conversation_ref="")) != ""
    assert compute_fingerprint_for(TurnIntakeConfig(), None) == ""


def test_turn_queue_layer_end_to_end() -> None:
    queue = TurnQueue(window_s=2.0)
    assert queue.turn_state("c1") == "none"

    first = queue.intake(conversation_ref="c1", text="hello", kind="direct", external_id="m1")
    assert first["action"] == "queue"
    second = queue.intake(conversation_ref="c1", text="and this", kind="direct", external_id="m2")
    assert second["action"] == "queue"
    assert "and this" in second["text"]
    assert queue.turn_state("c1") == "pending"

    # A client retry of m1 must not open a second turn.
    assert queue.intake(conversation_ref="c1", text="hello", external_id="m1")["action"] == "drop"

    queue.mark_running("c1", "bot-1")
    steer = queue.intake(conversation_ref="c1", text="actually stop", external_id="m3")
    assert steer["action"] == "steer"
    assert steer["state"] == STATE_DELIVERED
    assert queue.steer_log("c1") == ["actually stop"]
    assert queue.running_snapshot()[0]["bot_ref"] == "bot-1"

    nxt = queue.next_turn("c1")
    assert nxt is not None and nxt["count"] == 2
    queue.mark_finished("c1")
    assert queue.next_turn("c1") is None


def test_turn_queue_drops_the_least_important_member_at_the_cap() -> None:
    queue = TurnQueue(window_s=60.0, max_pending=3)
    queue.intake(conversation_ref="c1", text="a", kind="direct", external_id="1", arrived_at=1.0)
    queue.intake(conversation_ref="c1", text="b", kind="channel", external_id="2", arrived_at=1.1)
    queue.intake(conversation_ref="c1", text="c", kind="routine", external_id="3", arrived_at=1.2)
    queue.intake(conversation_ref="c1", text="d", kind="direct", external_id="4", arrived_at=1.3)
    kinds = [str(item.get("kind")) for item in queue.list_pending("c1")]
    texts = [str(item.get("text")) for item in queue.list_pending("c1")]
    assert kinds.count("routine") == 0
    assert len(kinds) == 3
    # The routine envelope is the one that was dropped, not the newest one.
    assert "c" not in texts
    assert "d" in texts


def test_turn_intake_trail_is_recorded() -> None:
    trail: list[dict[str, object]] = []
    turn_intake(
        TurnIntakeConfig(turn_state=lambda: "none", trail_sink=trail.append),
        TurnIntakeInput(conversation_ref="c1", kind="direct", text="x", arrived_at=1.0),
        None,
        now=1.0,
    )
    assert any(entry.get("event") == "intake.queued" for entry in trail)
    assert json.dumps(trail)  # trail entries must be JSON-serialisable