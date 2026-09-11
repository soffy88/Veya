from __future__ import annotations

import pytest

from runtime.knowledge_reliability import Evidence, KnowledgeRuntime, RetrievalPlan
from runtime.provider_reliability import ReliableProviderAdapter


def ev(eid: str, source: str, claim: str, polarity: str = "支持", *, authority: float = 0.8):
    return Evidence(eid, "q", source, claim, claim, authority, 0.9, 0.7, source, polarity)


@pytest.mark.asyncio
async def test_retrieval_plan_and_duplicate_suppression():
    runtime = KnowledgeRuntime(RetrievalPlan("q", ["claim"], ["web"], retrieval_budget=3))
    calls = 0

    async def retrieve(*_args):
        nonlocal calls
        calls += 1
        return [ev("e1", "a", "claim")]

    await runtime.retrieve(retrieve)
    await runtime.retrieve(retrieve)
    assert runtime.plan.question == "q"
    assert runtime.suppressed_retrievals >= 1
    assert calls == 1


@pytest.mark.asyncio
async def test_insufficient_evidence_triggers_targeted_retrieval():
    runtime = KnowledgeRuntime(RetrievalPlan("q", ["target"], ["web"], depth=2, retrieval_budget=3))
    queries: list[str] = []

    async def retrieve(query, *_args):
        queries.append(query)
        return [ev("e2", "b", "target")] if query == "targeted" else []

    result = await runtime.retrieve(retrieve, next_query=lambda *_: "targeted")
    assert result.status == "PASS"
    assert queries == ["q", "targeted"]


def test_conflicting_sources_are_preserved_and_scored():
    runtime = KnowledgeRuntime(RetrievalPlan("q", ["claim"]))
    runtime.add_evidence(ev("e1", "official", "claim", "支持", authority=1.0))
    runtime.add_evidence(ev("e2", "independent", "claim", "反对"))
    result = runtime.result()
    assert len(result.contradictions) == 1
    assert set(result.contradictions[0]["evidence_refs"]) == {"e1", "e2"}
    assert result.source_diversity == 2
    assert result.evidence_scores["e1"] > result.evidence_scores["e2"]


@pytest.mark.asyncio
async def test_retrieval_budget_returns_partial_not_fake_success():
    runtime = KnowledgeRuntime(RetrievalPlan("q", ["missing"], ["web"], retrieval_budget=1))

    async def retrieve(*_args):
        return []

    assert (await runtime.retrieve(retrieve)).status == "PARTIAL"


def test_synthesis_requires_real_evidence_refs():
    runtime = KnowledgeRuntime(RetrievalPlan("q"))
    runtime.add_evidence(ev("e1", "source", "claim"))
    assert runtime.synthesize("answer", ["e1"]).evidence_refs == ["e1"]
    assert runtime.synthesize("invented", []).status == "BLOCKED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [TimeoutError("timeout"), RuntimeError("503 server error"), ValueError("bad")]
)
async def test_provider_errors_fail_over(failure):
    adapter = ReliableProviderAdapter(failure_threshold=3)

    async def request(name):
        if name == "primary":
            raise failure
        return {"ok": True}

    name, result, continuity = await adapter.call(
        request, ["primary", "fallback"], goal_run_id="goal", context={"step": "s"}
    )
    assert name == "fallback" and result["ok"]
    assert continuity["goal_run_id"] == "goal"


@pytest.mark.asyncio
async def test_malformed_and_empty_responses_fail_over():
    adapter = ReliableProviderAdapter(failure_threshold=3)

    async def request(name):
        return "" if name == "primary" else {"ok": True}

    name, _, _ = await adapter.call(request, ["primary", "fallback"], goal_run_id="g", context={})
    assert name == "fallback"
    assert adapter.health["primary"].empty == 1


@pytest.mark.asyncio
async def test_unhealthy_provider_is_avoided():
    adapter = ReliableProviderAdapter(failure_threshold=1)

    async def fail(_name):
        raise TimeoutError("timeout")

    with pytest.raises(RuntimeError):
        await adapter.call(fail, ["bad"], goal_run_id="g", context={})
    assert adapter.select(["bad", "good"]) == ["good"]


@pytest.mark.asyncio
async def test_provider_switch_preserves_same_goalrun_context_and_verification():
    adapter = ReliableProviderAdapter(failure_threshold=3)
    context = {
        "plan": ["research"],
        "evidence_refs": ["e1"],
        "current_step": "research",
        "verification_spec": "vs-1",
    }

    async def request(name):
        if name == "p1":
            raise TimeoutError("timeout")
        return {"verdict": "PASS", "evidence_refs": ["e1"]}

    name, result, continuity = await adapter.call(
        request,
        ["p1", "p2"],
        requirements={"capability": "knowledge", "context_size": 1000, "tools": True},
        goal_run_id="goal-1",
        context=context,
    )
    assert name == "p2"
    assert continuity == {"goal_run_id": "goal-1", "context": context}
    assert result["verdict"] == "PASS"
    assert result["evidence_refs"] == context["evidence_refs"]
