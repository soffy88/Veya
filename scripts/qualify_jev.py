#!/usr/bin/env python3
"""Real Jev qualification (spec §34) against OpenCode Zen.

    venv/bin/python scripts/qualify_jev.py

Provider discovery and real calls are BLOCKED_PROVIDER_AUTH when Zen rejects the
credential or the free model is absent — never faked as PASS. The adapter,
fallback and mock contract tests live in tests/decision/.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    status = "PASS" if ok else "BLOCKED_PROVIDER_AUTH"
    RESULTS.append((name, status, note))
    print(f"{name}={status}" + (f"  # {note}" if note else ""))


def _load_env_key() -> None:
    """Load OPENCODE_API_KEY from the repo .env when not already in the env.

    The value is never printed.
    """

    if os.environ.get("OPENCODE_ZEN_API_KEY") or os.environ.get("OPENCODE_API_KEY"):
        return
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("OPENCODE_API_KEY="):
            os.environ["OPENCODE_API_KEY"] = line.split("=", 1)[1].strip()
            return


async def main() -> int:
    from veya.decision import (
        JevAnswer,
        JevDecision,
        JevDecisionPlane,
        JevPolicy,
        JevQuestion,
        JevUnavailable,
        QuestionKind,
        needs_supervisor,
    )

    _load_env_key()
    plane = JevDecisionPlane.create(policy=JevPolicy())

    # 1. provider discovery
    probe = await plane.probe()
    record(
        "JEV_PROVIDER_DISCOVERY",
        bool(probe.get("available")),
        f"reason={probe.get('reason')} key_source={probe.get('key_source')} models={probe.get('models')}",
    )
    if not probe.get("available"):
        print("JEV_REAL_CALL=BLOCKED_PROVIDER_AUTH")
        print("JEV_CHOICE=BLOCKED_PROVIDER_AUTH")
        print("JEV_SCORE=BLOCKED_PROVIDER_AUTH")
        print("JEV_NOUL=BLOCKED_PROVIDER_AUTH")
        print("JEV_FAST_PATH=BLOCKED_PROVIDER_AUTH")
        print("FINAL: BLOCKED_PROVIDER_AUTH (adapter + mocks remain separate)")
        return 2

    # 2. real call: one fan-out, three question kinds (fast path)
    questions = [
        JevQuestion("choice", QuestionKind.choice, criteria={"yes": "proceed", "no": "stop"}),
        JevQuestion("score", QuestionKind.score, criteria=["no risk", "high risk"]),
        JevQuestion("noul", QuestionKind.noul, instructions="Is any blocker present?"),
    ]
    try:
        decision = await plane.decide({"goal": "qualify jev", "iteration": 1}, questions)
    except JevUnavailable as exc:
        record("JEV_REAL_CALL", False, exc.code)
        return 2

    record("JEV_REAL_CALL", True, f"model={decision.model}")
    record("JEV_FAST_PATH", len(questions) == 3 and bool(decision.answers), "single fan-out")
    record(
        "JEV_CHOICE",
        decision.answers.get("choice", JevAnswer("", QuestionKind.choice)).choice is not None,
    )
    record(
        "JEV_SCORE",
        decision.answers.get("score", JevAnswer("", QuestionKind.score)).score is not None,
    )
    record(
        "JEV_NOUL", decision.answers.get("noul", JevAnswer("", QuestionKind.noul)).noul is not None
    )

    # 3. low-confidence escalation
    low = JevDecision(answers={"q": JevAnswer("q", QuestionKind.score, score=0.5, confidence=0.1)})
    record("JEV_LOW_CONFIDENCE_ESCALATION", needs_supervisor(low) is True)

    # 4. provider failure -> supervisor fallback
    class _Broken:
        async def probe(self) -> set[str]:
            raise JevUnavailable("JEV_PROVIDER_AUTH")

        async def ask(self, payload: dict) -> dict:
            raise JevUnavailable("JEV_QUOTA")

    async def _fallback(state: dict, qs: list) -> JevDecision:
        return JevDecision(model="supervisor-llm")

    broken = JevDecisionPlane.create(
        transport=_Broken(), policy=JevPolicy(), environ={"OPENCODE_AUTH_JSON": "/nonexistent"}
    )
    broken.fallback = _fallback
    fell_back = await broken.decide_or_fallback({}, [JevQuestion("q", QuestionKind.score)])
    record("JEV_PROVIDER_FAILURE_FALLBACK", fell_back.source == "provider_fallback")

    failed = [n for n, s, _ in RESULTS if s != "PASS"]
    print("\n" + "=" * 60)
    print("JEV QUALIFICATION PASSED" if not failed else f"JEV QUALIFICATION NOT PASSED: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
