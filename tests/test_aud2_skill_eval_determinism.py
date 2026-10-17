"""AUD2: the skill evaluation gate must be deterministic and CPU-only by default.

Root cause under test: ``default_eval_runner`` silently attempted a live LLM call
whenever a provider happened to be reachable, so the gate's verdict (and the
pass-case of tests/test_gbp6_evoontology.py) depended on network state and on
whatever wording the model returned. A gate that spends paid tokens and changes
verdict per run cannot be used as a release sensor.
"""

from __future__ import annotations

import pytest

from server.skill_eval_gate import default_eval_runner


@pytest.mark.asyncio
async def test_default_eval_runner_does_not_call_llm_by_default(monkeypatch):
    """Without the explicit opt-in flag, no LLM call happens (CPU-only, deterministic)."""
    import veya.llm as llm_mod

    calls: list[object] = []

    async def _boom(*args, **kwargs):  # pragma: no cover - must never run
        calls.append((args, kwargs))
        raise AssertionError("default_eval_runner must not call the LLM by default")

    monkeypatch.setattr(llm_mod, "llm_call", _boom, raising=False)
    monkeypatch.delenv("VEYA_SKILL_EVAL_LIVE_LLM", raising=False)

    task = {"task_id": "t1", "input": "Summarize the document", "expected": "", "budget": 3}
    skill = {"name": "auto_summarizer", "description": "Summarize document cleanly"}

    result = await default_eval_runner(task=task, skill=skill, budget=3)

    assert calls == []
    assert result["completed"] is True
    assert "auto_summarizer" in result["output"]


@pytest.mark.asyncio
async def test_default_eval_runner_is_repeatable(monkeypatch):
    """Two runs over the same inputs produce identical outputs (determinism)."""
    monkeypatch.delenv("VEYA_SKILL_EVAL_LIVE_LLM", raising=False)
    task = {"task_id": "t1", "input": "Compute total", "expected": "110", "budget": 3}
    skill = {"name": "calculator", "description": "Compute totals with VAT"}

    first = await default_eval_runner(task=task, skill=skill, budget=3)
    second = await default_eval_runner(task=task, skill=skill, budget=3)

    assert first["output"] == second["output"]
    assert first["completed"] is True
