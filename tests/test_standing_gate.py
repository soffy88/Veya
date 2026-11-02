"""Standing gate (item 1): declined rules refuse questions before anyone is
asked; decided rules are injected verbatim; history is never rewritten."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import decision_ledger as dl_mod
from server.bot_team.attention import AttentionStore, StandingRefusedError
from server.bot_team.turns import build_objective
from server.decision_ledger import DecisionLedger
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.routes.bot_team import router as bots_router

USER = {"user_id": "standing-user", "username": "standing"}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_STANDING_GATE", "1")
    return DecisionLedger(tmp_path / "ledger.db")


@pytest.fixture
def gate_off(monkeypatch):
    monkeypatch.setenv("VEYA_STANDING_GATE", "0")


@pytest.fixture
def att_env(tmp_path, monkeypatch):
    bot_root = tmp_path / "bots-root"
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(bot_root))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    return {"root": bot_root}


@pytest.fixture
def client():
    app = FastAPI()
    from server import auth as _w11_auth

    app.dependency_overrides[_w11_auth.require_user] = lambda: dict(USER)
    app.include_router(bots_router)
    return TestClient(app)


def _register(registry: BotRegistry, *bot_ids: str) -> BotRegistry:
    for bot_id in bot_ids:
        registry.register(
            BotSpec(
                bot_id=bot_id,
                owner_id=USER["user_id"],
                name=bot_id.capitalize(),
                description=f"Does {bot_id} work.",
                instructions=f"You only do {bot_id} work.",
            )
        )
    return registry


# ── ledger ────────────────────────────────────────────────────────────


def test_record_find_declined(ledger):
    sid = ledger.record_standing(
        "declined", "不要删数据库", scope="删数据", source="test"
    )
    rows = ledger.find_standing("能不能删数据库？")
    assert [r["id"] for r in rows] == [sid]
    assert rows[0]["verdict"] == "declined"
    assert rows[0]["statement"] == "不要删数据库"


def test_generic_shared_word_does_not_match(ledger):
    ledger.record_standing("declined", "不要删数据库", scope="删数据")
    assert ledger.find_standing("数据报表怎么做？") == []


def test_empty_scope_needs_more_hits(ledger):
    ledger.record_standing("declined", "不要删数据库", scope="")
    assert ledger.find_standing("数据报表怎么做？") == []
    assert ledger.find_standing("能不能删数据库？") != []


def test_exact_scope_replacement_supersedes(ledger):
    first = ledger.record_standing("declined", "旧规矩", scope="删数据")
    second = ledger.record_standing("declined", "新规矩", scope="删数据")
    live = ledger.list_standing()
    assert [r["id"] for r in live] == [second]
    assert ledger.find_standing("能不能删数据库")[0]["statement"] == "新规矩"
    assert first != second


def test_explicit_supersede_keeps_history(ledger):
    sid = ledger.record_standing("decided", "用方案 A", scope="选型")
    assert ledger.supersede_standing(sid) is True
    assert ledger.list_standing() == []
    assert ledger.find_standing("选型方案 A") == []
    assert ledger.supersede_standing(sid) is False


def test_standing_summary_empty_and_full(ledger):
    assert ledger.standing_summary() == ""
    ledger.record_standing("decided", "周报周五发", scope="周报", category="publish-external")
    ledger.record_standing("declined", "不要动生产库", scope="删数据")
    summary = ledger.standing_summary()
    assert "周报周五发" in summary
    assert "不要动生产库" in summary
    assert "⛔" in summary and "✅" in summary


def test_record_standing_validation(ledger):
    with pytest.raises(ValueError, match="verdict"):
        ledger.record_standing("maybe", "x")
    with pytest.raises(ValueError, match="statement"):
        ledger.record_standing("decided", "   ")


# ── gate helper ───────────────────────────────────────────────────────


def test_check_standing_flag_off(ledger, gate_off, monkeypatch):
    ledger.record_standing("declined", "不要删数据库", scope="删数据")
    assert dl_mod.check_standing("删数据库", ledger=ledger) is None


def test_check_standing_fail_open(monkeypatch):
    monkeypatch.setenv("VEYA_STANDING_GATE", "1")

    class Broken:
        def find_standing(self, *a, **k):
            raise RuntimeError("boom")

    assert dl_mod.check_standing("删数据库", ledger=Broken()) is None


# ── AttentionStore gate ───────────────────────────────────────────────


def test_create_refused_by_declined(att_env, ledger, monkeypatch):
    monkeypatch.setattr(dl_mod, "ledger", ledger)
    ledger.record_standing("declined", "不要删数据库", scope="删数据")
    store = AttentionStore(att_env["root"], ledger=ledger)
    with pytest.raises(StandingRefusedError) as exc:
        store.create(question="能不能删数据库？", bot_id="alpha")
    assert exc.value.standing["statement"] == "不要删数据库"
    assert store.list(status="open") == []


def test_create_decided_appends_context(att_env, ledger, monkeypatch):
    monkeypatch.setattr(dl_mod, "ledger", ledger)
    ledger.record_standing("decided", "周报周五发", scope="周报")
    store = AttentionStore(att_env["root"], ledger=ledger)
    item = store.create(question="周报什么时候发？", bot_id="alpha")
    assert "[定规] 周报周五发" in (item["context"] or "")
    assert item["standing_id"]


def test_gate_question_only(att_env, ledger, monkeypatch):
    monkeypatch.setattr(dl_mod, "ledger", ledger)
    ledger.record_standing("declined", "不要删数据库", scope="删数据")
    store = AttentionStore(att_env["root"], ledger=ledger)
    item = store.create(
        question="委托失败：删数据库连试 3 次",
        bot_id="alpha",
        kind="incident",
    )
    assert item["status"] == "open"
    esc = store.create(
        question="房内发帖无人接：删数据库",
        group_id="g1",
        kind="escalation",
    )
    assert esc["status"] == "open"


def test_bypass_standing(att_env, ledger, monkeypatch):
    monkeypatch.setattr(dl_mod, "ledger", ledger)
    ledger.record_standing("declined", "不要删数据库", scope="删数据")
    store = AttentionStore(att_env["root"], ledger=ledger)
    item = store.create(
        question="能不能删数据库？", bot_id="alpha", bypass_standing=True
    )
    assert item["status"] == "open"


def test_gate_off_passes(att_env, ledger, gate_off, monkeypatch):
    monkeypatch.setattr(dl_mod, "ledger", ledger)
    ledger.record_standing("declined", "不要删数据库", scope="删数据")
    store = AttentionStore(att_env["root"], ledger=ledger)
    item = store.create(question="能不能删数据库？", bot_id="alpha")
    assert item["status"] == "open"


# ── routes ────────────────────────────────────────────────────────────


def test_route_refused_returns_standing(client, att_env, ledger, monkeypatch):
    monkeypatch.setattr(dl_mod, "ledger", ledger)
    _register(BotRegistry(), "alpha")
    ledger.record_standing("declined", "不要删数据库", scope="删数据")
    r = client.post(
        "/api/v1/bots/attention",
        json={"question": "能不能删数据库？", "bot_id": "alpha"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "refused_by_standing"
    assert r.json()["standing"]["statement"] == "不要删数据库"
    assert r.json()["item"] is None
    assert client.get("/api/v1/bots/attention").json()["attention"] == []


def test_route_answer_records_standing(client, att_env, ledger, monkeypatch):
    monkeypatch.setattr(dl_mod, "ledger", ledger)
    _register(BotRegistry(), "alpha")
    r = client.post(
        "/api/v1/bots/attention",
        json={"question": "周报什么时候发？", "bot_id": "alpha"},
    )
    item_id = r.json()["item"]["id"]
    r2 = client.post(
        f"/api/v1/bots/attention/{item_id}/answer",
        json={"answer": "周五发", "record_standing": True},
    )
    assert r2.status_code == 200
    live = ledger.list_standing()
    assert len(live) == 1
    assert live[0]["statement"] == "周五发"
    assert live[0]["verdict"] == "decided"
    assert "周报" in live[0]["scope"]


# ── turns injection ───────────────────────────────────────────────────


def test_build_objective_injects_standing(ledger, monkeypatch):
    monkeypatch.setattr(dl_mod, "ledger", ledger)
    monkeypatch.setenv("VEYA_STANDING_GATE", "1")
    ledger.record_standing("decided", "周报周五发", scope="周报")
    spec = BotSpec(
        bot_id="alpha",
        owner_id=USER["user_id"],
        name="alpha",
        description="Does alpha work.",
        instructions="You only do alpha work.",
    )
    objective = build_objective(bot=spec, text="hello")
    assert "veya:standing" in objective
    assert "周报周五发" in objective


def test_build_objective_empty_without_standing(ledger, monkeypatch):
    monkeypatch.setattr(dl_mod, "ledger", ledger)
    monkeypatch.setenv("VEYA_STANDING_GATE", "1")
    spec = BotSpec(
        bot_id="alpha",
        owner_id=USER["user_id"],
        name="alpha",
        description="Does alpha work.",
        instructions="You only do alpha work.",
    )
    objective = build_objective(bot=spec, text="hello")
    assert "veya:standing" not in objective


# ── project_ask helper ────────────────────────────────────────────────


def test_decision_tools_end_to_end(tmp_path, monkeypatch):
    """The registered tools themselves: record + query standing actions."""
    import asyncio

    from server.tool_registry import MasterToolRegistry, _register_internalized_tools

    monkeypatch.setenv("VEYA_STANDING_GATE", "1")
    monkeypatch.setattr(dl_mod, "ledger", DecisionLedger(tmp_path / "tools.db"))
    mt = MasterToolRegistry()
    _register_internalized_tools(mt)
    out = asyncio.run(
        mt.execute(
            "decision_record_standing",
            {"verdict": "declined", "statement": "不要删数据库", "scope": "删数据"},
        )
    )
    assert out.startswith("standing recorded: st_")
    out2 = asyncio.run(mt.execute("decision_query", {"action": "standing"}))
    assert "不要删数据库" in out2


def test_standing_match_for_ask(ledger, monkeypatch):
    from server.project_ask import _standing_match_for

    monkeypatch.setattr(dl_mod, "ledger", ledger)
    ledger.record_standing("declined", "不要删数据库", scope="删数据")
    match = _standing_match_for("能不能删数据库？", ["删哪个库？"], ledger=ledger)
    assert match is not None and match["verdict"] == "declined"
    assert _standing_match_for("今晚吃什么？", [], ledger=ledger) is None
