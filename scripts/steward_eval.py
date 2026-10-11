#!/usr/bin/env python3
"""steward_eval — offline evaluation for the context steward (NANO-W10 ADDENDUM M).

Hand-written conversation endings, a scripted or heuristic stand-in for the
model, and the same parse the production path uses. It answers one question:
*given this ending, does the steward pick CHECKPOINT or HANDOFF, and does the
output survive a broken model reply?*

By default it is **mock**: no network, no key, deterministic. Point it at a real
model with ``--live`` to see how the actual small model behaves; the scoring is
identical either way.

Usage::

    python scripts/steward_eval.py                 # mock, built-in cases
    python scripts/steward_eval.py --live          # same cases, real LLM
    python scripts/steward_eval.py --case long-running-task --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from veya.omodul.context_steward import (  # noqa: E402
    ContextStewardConfig,
    ContextStewardInput,
    context_steward,
)
from veya.oskill.pure.steward_decide import CHECKPOINT, HANDOFF  # noqa: E402


def _msg(role: str, text: str) -> dict[str, str]:
    return {"role": role, "content": text}


#: Each case is one hand-written conversation ending plus what the steward
#: should decide. ``why`` is recorded in the report so a regression is readable.
CASES: list[dict[str, Any]] = [
    {
        "name": "short-conversation",
        "tokens": 900,
        "expected": "none",
        "why": "below the check line: no decision is warranted at all",
        "messages": [
            _msg("user", "hi"),
            _msg("assistant", "hello — what can I do?"),
        ],
    },
    {
        "name": "long-running-task",
        "tokens": 7_400,
        "expected": CHECKPOINT,
        "why": "long but the work continues here: condense in place",
        "messages": [
            _msg("user", "set up the migration pipeline"),
            _msg("assistant", "started; 3 of 9 steps done"),
            _msg("user", "keep going"),
            _msg("assistant", "6 of 9 steps done, no errors so far"),
            _msg("user", "keep going"),
            _msg("assistant", "9 of 9 steps done, running the backfill"),
        ],
    },
    {
        "name": "clean-stopping-point",
        "tokens": 8_100,
        "expected": HANDOFF,
        "why": "the user wrapped up and the next request is a different job",
        "messages": [
            _msg("user", "fix the flaky test"),
            _msg("assistant", "fixed it; suite is green"),
            _msg("user", "great, that is all I needed today"),
            _msg("assistant", "done — nothing left running"),
        ],
    },
]


def mock_llm(case_name: str, threshold: str) -> dict[str, Any]:
    """Deterministic stand-in for the small steward model.

    Keys off the case name so the same case always produces the same reply;
    ``--broken`` mode returns garbage so the parser's failure path is exercised.
    """
    reply = {
        "long-running-task": {
            "action": CHECKPOINT,
            "summary": "Migration pipeline set up; backfill still running.",
            "verbatim": "keep going",
            "reason": "work continues in this part",
        },
        "clean-stopping-point": {
            "action": HANDOFF,
            "summary": "Flaky test fixed, suite green. Nothing running.",
            "verbatim": "",
            "reason": "the user closed the task",
        },
    }.get(case_name)
    if reply is None:
        reply = {
            "action": CHECKPOINT,
            "summary": "",
            "verbatim": "",
            "reason": "default",
        }
    return {"content": json.dumps(reply), "cost_usd": 0.0}


def broken_llm(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """A model that returns prose instead of JSON — the parse must not crash."""
    return {"content": "I think you should probably condense it, whatever.", "cost_usd": 0.0}


def live_llm(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Real provider call (only used with ``--live``)."""
    import veya.obase.llm as llm

    async def _call() -> dict[str, Any]:
        response = await llm.llm_call(messages, max_tokens=400)
        choices = response.get("choices") or [{}]
        message = choices[0].get("message") or {} if choices else {}
        return {"content": str(message.get("content") or ""), "usage": response.get("usage") or {}}

    return asyncio.run(_call())


async def run_case(case: dict[str, Any], *, live: bool, broken: bool) -> dict[str, Any]:
    """Run one case end-to-end through the real omodul."""
    caller = broken_llm if broken else (live_llm if live else None)
    config = ContextStewardConfig(
        llm_caller=caller if broken or live else (lambda _m: mock_llm(case["name"], "check")),
        window=10_000,
        reply_reserve=2_000,
        check_ratio=0.8,
        hard_ratio=0.95,
    )
    out = await context_steward(
        config,
        ContextStewardInput(
            session_ref=f"eval:{case['name']}",
            part=1,
            messages=case["messages"],
            tokens=int(case["tokens"]),
        ),
    )
    actual = out.get("action")
    expected = case["expected"]
    if broken:
        # A garbage reply must degrade to CHECKPOINT — never to a silent no-op
        # and never to HANDOFF.  A case that never reaches the model (below the
        # check line) still expects "none": no reply was asked for.
        expectation = CHECKPOINT if expected != "none" else "none"
        passed = actual == expectation
    else:
        expectation = expected
        passed = actual == expected
    return {
        "case": case["name"],
        "expected": expectation,
        "actual": actual,
        "passed": passed,
        "threshold": out.get("threshold"),
        "next_part": out.get("next_part"),
        "summary": out.get("summary", "")[:80],
        "why": case["why"],
        "status": out.get("status"),
        "error": out.get("error"),
    }


async def run_all(*, live: bool, broken: bool) -> tuple[list[dict[str, Any]], bool]:
    rows = [await run_case(case, live=live, broken=broken) for case in CASES]
    return (rows, all(row["passed"] for row in rows))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline context-steward eval")
    parser.add_argument("--live", action="store_true", help="call the real model instead of the mock")
    parser.add_argument("--broken", action="store_true", help="make the model return garbage on purpose")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.live and not os.environ.get("OPENCODE_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        print("note: no API key visible; the live call will degrade to a stub reply", file=sys.stderr)

    rows, ok = asyncio.run(run_all(live=args.live, broken=args.broken))
    if args.json:
        print(json.dumps({"ok": ok, "cases": rows}, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    width = max(len(row["case"]) for row in rows)
    print(f"{'case'.ljust(width)}  expected   actual     result")
    for row in rows:
        mark = "PASS" if row["passed"] else "FAIL"
        print(
            f"{row['case'].ljust(width)}  {row['expected']:<10} "
            f"{str(row['actual']):<10}  {mark}"
        )
        if args.verbose:
            print(f"    why: {row['why']}")
            if row.get("error"):
                print(f"    error: {row['error']}")
    print(f"\n{sum(1 for row in rows if row['passed'])}/{len(rows)} passed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())