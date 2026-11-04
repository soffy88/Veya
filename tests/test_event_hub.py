"""Tests for EventHub ingest: dedupe, rules, single-flight, parallel, tell."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from server.nano_w10.event_hub import EventHub, EventSeenStore


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now_value = start

    def now(self) -> float:
        return self.now_value

    def advance(self, seconds: float) -> None:
        self.now_value += seconds


def _skills(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    (skills / "hub-feedback").mkdir(parents=True)
    (skills / "hub-feedback" / "manifest.json").write_text("{}", encoding="utf-8")
    return skills


def _rule(**overrides):
    rule = {
        "name": "r1",
        "when": {"source": "virxact-hub", "types": ["feedback.new"]},
        "to": "bot-1",
        "skill": "hub-feedback",
        "ask": "Handle {summary}.",
        "group": "hub-feedback",
        "parallel": 3,
        "tell": "silent",
        "timeout_s": 1800,
    }
    rule.update(overrides)
    return rule


def _hub(tmp_path: Path, *, clock: FakeClock | None = None, enabled: bool = True, **kwargs):
    skills = _skills(tmp_path)
    rules_path = tmp_path / "event_rules.json"
    seen_path = tmp_path / "event_hub.db"
    events: list[tuple[str, dict]] = []

    def record(topic, payload):
        events.append((topic, payload))

    hub = EventHub(
        rules_path=str(rules_path),
        seen_store=EventSeenStore(str(seen_path)),
        record_event=record,
        bot_exists=lambda bid: bid == "bot-1",
        skills_dir=skills,
        time_fn=clock.now if clock is not None else None,
        enabled_fn=lambda: enabled,
        **kwargs,
    )
    return hub, rules_path, events


def _write_rules(rules_path: Path, rules) -> None:
    rules_path.write_text(json.dumps({"version": 1, "rules": rules}), encoding="utf-8")


def _envelope(**overrides):
    env = {
        "event_id": "evt-1",
        "source": "virxact-hub",
        "type": "feedback.new",
        "subject": "obj-1",
        "summary": "hello",
        "link": "http://x/1",
    }
    env.update(overrides)
    return env


async def _fake_dispatch(calls, *, delay: float = 0.0):
    async def dispatch(envelope, rule):
        calls.append(("start", envelope["event_id"]))
        if delay:
            await asyncio.sleep(delay)
        calls.append(("end", envelope["event_id"]))
        return {"ok": True}

    return dispatch


async def test_flag_off_is_passthrough(tmp_path):
    hub, _rules_path, events = _hub(tmp_path, enabled=False)
    calls: list = []
    result = await hub.ingest(_envelope(), dispatch=await _fake_dispatch(calls))
    assert result["status"] == "passthrough"
    assert calls == []
    assert events == []
    assert hub._seen.count_for("evt-1") == 0


async def test_duplicate_within_window_runs_once(tmp_path):
    clock = FakeClock()
    hub, rules_path, _events = _hub(tmp_path, clock=clock)
    _write_rules(rules_path, [_rule()])
    calls: list = []
    dispatch = await _fake_dispatch(calls)
    first = await hub.ingest(_envelope(), dispatch=dispatch)
    second = await hub.ingest(_envelope(), dispatch=dispatch)
    third = await hub.ingest(_envelope(), dispatch=dispatch)
    assert first["status"] == "accepted"
    assert second["status"] == "duplicate" and second["count"] == 2
    assert third["status"] == "duplicate" and third["count"] == 3
    assert len([c for c in calls if c[0] == "start"]) == 1


async def test_duplicate_window_expiry_reaccepts(tmp_path):
    clock = FakeClock()
    hub, rules_path, _events = _hub(tmp_path, clock=clock)
    _write_rules(rules_path, [_rule()])
    dispatch = await _fake_dispatch([])
    await hub.ingest(_envelope(), dispatch=dispatch)
    clock.advance(601)
    result = await hub.ingest(_envelope(), dispatch=dispatch)
    assert result["status"] == "accepted"


async def test_no_rule_drops_without_dispatch(tmp_path):
    hub, rules_path, events = _hub(tmp_path)
    _write_rules(rules_path, [_rule()])
    calls: list = []
    result = await hub.ingest(_envelope(type="other.thing"), dispatch=await _fake_dispatch(calls))
    assert result["status"] == "dropped_no_rule"
    assert calls == []
    assert any(topic == "event.dropped_no_rule" for topic, _ in events)


async def test_bad_envelope_refused(tmp_path):
    hub, _rules_path, _events = _hub(tmp_path)
    result = await hub.ingest({"source": "", "type": "x"}, dispatch=await _fake_dispatch([]))
    assert result["status"] == "refused"


async def test_seen_store_failure_is_unavailable(tmp_path):
    class BrokenStore:
        def record(self, *args, **kwargs):
            raise RuntimeError("disk gone")

        def prune(self, *args, **kwargs):
            return 0

        def count_for(self, *args, **kwargs):
            return 0

    hub, _rules_path, _events = _hub(tmp_path)
    hub._seen = BrokenStore()
    result = await hub.ingest(_envelope(), dispatch=await _fake_dispatch([]))
    assert result["status"] == "unavailable"


async def test_bad_rules_file_keeps_previous(tmp_path):
    hub, rules_path, events = _hub(tmp_path)
    _write_rules(rules_path, [_rule()])
    assert hub.reload_rules()["reloaded"] is True
    rules_path.write_text("{not json", encoding="utf-8")
    result = hub.reload_rules()
    assert result["reloaded"] is False
    assert hub.rules_error is not None
    assert len(hub.rules_snapshot()) == 1
    assert any(topic == "event.rules_rejected" for topic, _ in events)
    rules_path.write_text(json.dumps({"rules": [_rule(to="ghost")]}), encoding="utf-8")
    assert hub.reload_rules()["reloaded"] is False
    assert len(hub.rules_snapshot()) == 1


async def test_missing_rules_file_keeps_previous(tmp_path):
    hub, rules_path, _events = _hub(tmp_path)
    _write_rules(rules_path, [_rule()])
    hub.reload_rules()
    rules_path.unlink()
    assert hub.reload_rules()["reloaded"] is False
    assert len(hub.rules_snapshot()) == 1


async def test_group_single_flight_serializes(tmp_path):
    hub, rules_path, _events = _hub(tmp_path)
    _write_rules(rules_path, [_rule(group="g1")])
    order: list[str] = []

    async def dispatch(envelope, rule):
        order.append(f"start-{envelope['event_id']}")
        await asyncio.sleep(0.05)
        order.append(f"end-{envelope['event_id']}")
        return {"ok": True}

    await asyncio.gather(
        hub.ingest(_envelope(event_id="e1"), dispatch=dispatch),
        hub.ingest(_envelope(event_id="e2"), dispatch=dispatch),
    )
    assert order in (
        ["start-e1", "end-e1", "start-e2", "end-e2"],
        ["start-e2", "end-e2", "start-e1", "end-e1"],
    )


async def test_parallel_cap_serializes_across_groups(tmp_path):
    hub, rules_path, _events = _hub(tmp_path)
    _write_rules(rules_path, [_rule(group="", parallel=1)])
    order: list[str] = []

    async def dispatch(envelope, rule):
        order.append(f"start-{envelope['event_id']}")
        await asyncio.sleep(0.05)
        order.append(f"end-{envelope['event_id']}")
        return {"ok": True}

    await asyncio.gather(
        hub.ingest(_envelope(event_id="e1"), dispatch=dispatch),
        hub.ingest(_envelope(event_id="e2"), dispatch=dispatch),
    )
    assert order in (
        ["start-e1", "end-e1", "start-e2", "end-e2"],
        ["start-e2", "end-e2", "start-e1", "end-e1"],
    )


async def test_parallel_two_runs_concurrently(tmp_path):
    hub, rules_path, _events = _hub(tmp_path)
    _write_rules(rules_path, [_rule(group="", parallel=2)])
    order: list[str] = []

    async def dispatch(envelope, rule):
        order.append(f"start-{envelope['event_id']}")
        await asyncio.sleep(0.05)
        order.append(f"end-{envelope['event_id']}")
        return {"ok": True}

    await asyncio.gather(
        hub.ingest(_envelope(event_id="e1"), dispatch=dispatch),
        hub.ingest(_envelope(event_id="e2"), dispatch=dispatch),
    )
    assert order.index("start-e2") < order.index("end-e1")


async def test_stale_queue_drops(tmp_path):
    hub, rules_path, events = _hub(tmp_path)
    _write_rules(rules_path, [_rule(group="g1", timeout_s=0.05)])
    started = asyncio.Event()

    async def slow(envelope, rule):
        started.set()
        await asyncio.sleep(0.3)
        return {"ok": True}

    first = asyncio.create_task(hub.ingest(_envelope(event_id="e1"), dispatch=slow))
    await started.wait()
    second = await hub.ingest(_envelope(event_id="e2"), dispatch=slow)
    assert second["status"] == "dropped_stale"
    assert any(topic == "event.dropped_stale" for topic, _ in events)
    assert (await first)["status"] == "accepted"


async def test_dispatch_failure_is_status_not_raise(tmp_path):
    hub, rules_path, events = _hub(tmp_path)
    _write_rules(rules_path, [_rule()])

    async def boom(envelope, rule):
        raise RuntimeError("kaboom")

    result = await hub.ingest(_envelope(), dispatch=boom)
    assert result["status"] == "failed"
    assert any(topic == "event.dispatch_failed" for topic, _ in events)


async def test_tell_needed_wakes_and_others_do_not(tmp_path):
    woken: list = []

    def wake(envelope, rule, run_id, result):
        woken.append(rule["name"])

    hub, rules_path, _events = _hub(tmp_path, wake=wake)
    _write_rules(rules_path, [_rule(name="loud", tell="needed")])
    await hub.ingest(_envelope(), dispatch=await _fake_dispatch([]))
    assert woken == ["loud"]
    _write_rules(rules_path, [_rule(name="quiet", tell="silent")])
    hub.reload_rules()
    await hub.ingest(_envelope(event_id="e2"), dispatch=await _fake_dispatch([]))
    assert woken == ["loud"]


async def test_default_bot_dispatch_splits_ask_and_materials(tmp_path, monkeypatch):
    hub, rules_path, _events = _hub(tmp_path)
    _write_rules(rules_path, [_rule()])
    captured: dict = {}

    async def fake_turn(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(task_id="t-1", status="done", reply="hi")

    fake_spec = SimpleNamespace(bot_id="bot-1", owner_id="u-1")

    class FakeRegistry:
        def get(self, bot_id):
            return fake_spec if bot_id == "bot-1" else None

    monkeypatch.setattr("server.goal_run.bot_identity.BotRegistry", FakeRegistry)
    monkeypatch.setattr("server.bot_team.turns.run_bot_turn", fake_turn)
    result = await hub.ingest(_envelope(), user={"user_id": "u-1"})
    assert result["status"] == "accepted"
    assert "hello" not in captured["text"]
    assert "summary: hello" in captured["materials"]
    assert captured["user"] == {"user_id": "u-1"}


async def test_default_bot_dispatch_refuses_without_user(tmp_path, monkeypatch):
    hub, rules_path, _events = _hub(tmp_path)
    _write_rules(rules_path, [_rule()])

    class FakeRegistry:
        def get(self, bot_id):
            return SimpleNamespace(bot_id="bot-1", owner_id="u-1")

    monkeypatch.setattr("server.goal_run.bot_identity.BotRegistry", FakeRegistry)
    result = await hub.ingest(_envelope())
    assert result["status"] == "failed"
