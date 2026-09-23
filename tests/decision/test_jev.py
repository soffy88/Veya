"""P4: Jev decision plane contract (mock transport; real call is qualified separately)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from veya.decision import (
    JevAnswer,
    JevDecision,
    JevDecisionPlane,
    JevPolicy,
    JevQuestion,
    JevUnavailable,
    QuestionKind,
    needs_supervisor,
    policy_from_env,
    resolve_api_key,
)


class FakeTransport:
    def __init__(
        self,
        models: set[str],
        answers: dict[str, Any] | None = None,
        error: Exception | None = None,
    ):
        self.models = models
        self.answers = answers or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def probe(self) -> set[str]:
        if self.error is not None:
            raise self.error
        return self.models

    async def ask(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        return {
            "model": payload["model"],
            "answers": self.answers.get(payload["model"], {}),
            "usage": {"input_tokens": 1},
            "cost": "0",
        }


# ── key resolution / policy ─────────────────────────────────────────────
def test_key_resolution_order(tmp_path: Path) -> None:
    assert resolve_api_key({"OPENCODE_ZEN_API_KEY": "zen", "OPENCODE_API_KEY": "go"}) == (
        "zen",
        "OPENCODE_ZEN_API_KEY",
    )
    assert resolve_api_key({"OPENCODE_API_KEY": "go"}) == ("go", "OPENCODE_API_KEY")
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"opencode": {"key": "from-file"}}), encoding="utf-8")
    assert resolve_api_key({"OPENCODE_AUTH_JSON": str(auth)}) == ("from-file", "auth.json")
    assert resolve_api_key({"OPENCODE_AUTH_JSON": str(tmp_path / "missing.json")}) == (None, None)


def test_paid_fallback_is_opt_in_only() -> None:
    assert policy_from_env({}).models() == ["jev-1.13-free"]
    assert policy_from_env({"JEV_ALLOW_PAID_FALLBACK": "1"}).models() == [
        "jev-1.13-free",
        "jev-1.13",
    ]


# ── readiness ───────────────────────────────────────────────────────────
async def test_probe_available_only_with_primary_model() -> None:
    plane = JevDecisionPlane.create(
        transport=FakeTransport({"jev-1.13-free"}), policy=JevPolicy(), environ={}
    )
    probe = await plane.probe()
    assert probe["available"] is True and probe["reason"] is None

    missing = JevDecisionPlane.create(
        transport=FakeTransport({"something-else"}), policy=JevPolicy(), environ={}
    )
    assert (await missing.probe())["reason"] == "JEV_MODEL_MISSING"


async def test_probe_auth_failure_is_fail_closed() -> None:
    plane = JevDecisionPlane.create(
        transport=FakeTransport(set(), error=JevUnavailable("JEV_PROVIDER_AUTH")),
        policy=JevPolicy(),
        environ={},
    )
    assert (await plane.probe())["reason"] == "JEV_PROVIDER_AUTH"


# ── decision ────────────────────────────────────────────────────────────
async def test_decide_parses_fanout_answers() -> None:
    answers = {
        "q_score": {"type": "score", "score": 0.2, "confidence": 0.8, "probabilities": {"0": 0.8}},
        "q_choice": {"type": "choice", "choice": "a", "confidence": 0.9},
        "q_noul": {"type": "noul", "noul": 0.1},
    }
    transport = FakeTransport({"jev-1.13-free"}, {"jev-1.13-free": answers})
    plane = JevDecisionPlane.create(transport=transport, policy=JevPolicy(), environ={})
    decision = await plane.decide(
        {"goal": "g"},
        [
            JevQuestion("q_score", QuestionKind.score, criteria=["no", "yes"]),
            JevQuestion("q_choice", QuestionKind.choice, criteria={"a": "first", "b": "second"}),
            JevQuestion("q_noul", QuestionKind.noul, instructions="blocker?"),
        ],
    )
    assert decision.source == "jev"
    assert decision.answers["q_choice"].choice == "a"
    assert decision.answers["q_score"].score == 0.2
    assert decision.answers["q_noul"].noul == 0.1
    # one fan-out call, model = free
    assert len(transport.calls) == 1 and transport.calls[0]["model"] == "jev-1.13-free"
    # wire shape: criteria dict for choice/score, instructions for noul
    wire = transport.calls[0]["questions"]
    assert wire["q_score"] == {"type": "score", "criteria": ["no", "yes"]}
    assert wire["q_noul"] == {"type": "noul", "instructions": "blocker?"}


async def test_free_failure_does_not_touch_paid_by_default() -> None:
    transport = FakeTransport(
        {"jev-1.13-free"},
        {"jev-1.13-free": {"q": {"type": "score", "score": 0.1}}},
        error=JevUnavailable("JEV_QUOTA"),
    )
    plane = JevDecisionPlane.create(transport=transport, policy=JevPolicy(), environ={})
    with pytest.raises(JevUnavailable):
        await plane.decide({}, [JevQuestion("q", QuestionKind.score, criteria=["x"])])
    assert [c["model"] for c in transport.calls] == ["jev-1.13-free"]


async def test_paid_attempted_only_when_opted_in() -> None:
    class FreeQuota(FakeTransport):
        async def ask(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(payload)
            if payload["model"] == "jev-1.13-free":
                raise JevUnavailable("JEV_QUOTA")
            return {
                "model": payload["model"],
                "answers": {"q": {"type": "score", "score": 0.5}},
                "cost": "1",
            }

    transport = FreeQuota(set())
    plane = JevDecisionPlane.create(
        transport=transport, policy=JevPolicy(allow_paid_fallback=True), environ={}
    )
    decision = await plane.decide({}, [JevQuestion("q", QuestionKind.score, criteria=["x"])])
    assert [c["model"] for c in transport.calls] == ["jev-1.13-free", "jev-1.13"]
    assert decision.model == "jev-1.13"


async def test_fallback_used_when_jev_unavailable(tmp_path: Path) -> None:
    async def fallback(state: dict, questions: list) -> JevDecision:
        return JevDecision(model="supervisor-llm")

    plane = JevDecisionPlane.create(
        transport=None,
        policy=JevPolicy(),
        environ={"OPENCODE_AUTH_JSON": str(tmp_path / "missing.json")},
        fallback=fallback,
    )
    assert plane.transport is None
    decision = await plane.decide_or_fallback({}, [JevQuestion("q", QuestionKind.score)])
    assert decision.source == "provider_fallback"


# ── calibration ─────────────────────────────────────────────────────────
def test_low_or_missing_confidence_needs_supervisor() -> None:
    low = JevDecision(answers={"q": JevAnswer("q", QuestionKind.score, score=0.1, confidence=0.2)})
    assert needs_supervisor(low) is True
    assert needs_supervisor(JevDecision()) is True  # unknown confidence -> review
