from __future__ import annotations

import pytest

from runtime.knowledge_reliability import Evidence, RetrievalPlan
from runtime.provider_reliability import ReliableProviderAdapter
from runtime.verification.models import VerificationSpec
from server.coordinator_master import _CANONICAL_TASK_CTX, _CAPABILITY_CTX, MasterCoordinator
from server.events import bind_event_capability, reset_event_capability
from server.goal_run.canonical_worker import CanonicalWorkerAdapter
from server.goal_run.leaf import LeafResult
from server.goal_run.models import GoalRunState, TaskNode, TaskStatus
from server.goal_run.runner import project_run_goal
from server.goal_run.verify import VerifyResult


class _VerificationStub:
    async def generate_verification_spec(self, task_id, goal_run_id, head_sha, *, feature_name):
        return VerificationSpec.create_for_task(task_id, goal_run_id, head_sha)


@pytest.mark.asyncio
async def test_canonical_worker_binds_spec_computer_and_context_across_resume(tmp_path):
    state = GoalRunState(goal_id="goal-integration", goal_text="integration goal")
    state.tasks["step"] = TaskNode(
        id="step",
        title="step",
        instruction="perform step",
        acceptance=["step completes"],
        depends_on=[],
        assignee="hicode",
        status=TaskStatus.ready,
    )
    adapter = CanonicalWorkerAdapter(
        task_id="task-integration",
        objective=state.goal_text,
        verification_engine=_VerificationStub(),
    )

    await adapter.before_execution(state, str(tmp_path))
    first_computer = adapter.computer_id
    assert first_computer
    assert adapter.spec.goal_run_id == state.goal_id
    assert adapter.context_engine.verify_integrity()
    assert adapter.context_engine.state.preserved.goal_run_id == state.goal_id
    assert adapter.context_engine.state.preserved.computer_id == first_computer
    assert (tmp_path / ".veya" / "runs" / state.goal_id / "context_checkpoint.json").exists()

    resumed = CanonicalWorkerAdapter(
        task_id="task-integration",
        objective=state.goal_text,
        verification_engine=_VerificationStub(),
    )
    await resumed.before_execution(state, str(tmp_path))
    assert resumed.computer_id == first_computer
    assert resumed.context_engine.state.goal_run_id == state.goal_id
    assert resumed.context_engine.verify_integrity()


@pytest.mark.asyncio
async def test_canonical_iteration_uses_knowledge_and_provider_continuity(tmp_path):
    calls = []

    async def retrieve(query, source_type, depth):
        calls.append(("retrieve", query, source_type, depth))
        return [Evidence("ev-1", query, "docs", "claim evidence", claim="claim")]

    async def provider(name):
        calls.append(("provider", name))
        if name == "provider-a":
            raise TimeoutError("provider timeout")
        return {"choices": [{"message": {"content": "ok"}}]}

    class Router:
        def select(self, candidates, requirements):
            return candidates

    state = GoalRunState(goal_id="goal-i1", goal_text="research goal")
    state.tasks["research"] = TaskNode(
        id="research",
        title="research",
        instruction="research",
        acceptance=["evidence"],
        depends_on=[],
        assignee="hicode",
        status=TaskStatus.ready,
    )
    adapter = CanonicalWorkerAdapter(
        task_id="task-i1",
        objective=state.goal_text,
        verification_engine=_VerificationStub(),
        provider_router=Router(),
        knowledge_plan=RetrievalPlan("question", ["claim"]),
        retriever=retrieve,
        provider_request=provider,
        provider_candidates=["provider-a", "provider-b"],
    )
    await adapter.before_execution(state, str(tmp_path))
    await adapter.before_iteration(state, str(tmp_path), state.tasks["research"])

    assert adapter.knowledge_result.evidence_refs == ["ev-1"]
    assert state.budget["provider"] == "provider-b"
    assert adapter.provider_response["choices"][0]["message"]["content"] == "ok"
    assert state.budget["provider_continuity"]["goal_run_id"] == state.goal_id
    assert calls == [("retrieve", "question", "web", 1), ("provider", "provider-a"), ("provider", "provider-b")]


@pytest.mark.asyncio
async def test_project_run_goal_wires_knowledge_and_provider_from_canonical_entry(
    tmp_path, monkeypatch
):
    """The public GoalRun entry, rather than the adapter alone, owns the hooks."""
    (tmp_path / ".veya-project").mkdir()
    monkeypatch.setenv("VEYA_GOAL_RUN_PLAN_REVIEW_ENABLED", "0")

    async def fake_leaf(*_args, **_kwargs):
        return LeafResult(status="completed", summary="worker used fallback", artifacts=[])

    async def fake_verify(*_args, **_kwargs):
        return VerifyResult(passed=True, summary="verified")

    monkeypatch.setattr("server.goal_run.runner.execute_leaf_with_memory", fake_leaf)
    monkeypatch.setattr("server.goal_run.runner.verify_task", fake_verify)
    calls = []

    async def retrieve(query, source_type, depth):
        calls.append(("retrieve", query, source_type, depth))
        return [Evidence("canonical-ev", query, "canonical-doc", "retrieved claim", claim="claim")]

    async def provider(name):
        calls.append(("provider", name))
        if name == "provider-a":
            raise TimeoutError("controlled timeout")
        return {"choices": [{"message": {"content": "fallback response"}}]}

    class Router:
        def select(self, candidates, requirements):
            return candidates

    capability_token = bind_event_capability("research")
    try:
        result = await project_run_goal(
            project_root=str(tmp_path),
            goal="research the canonical integration",
            tasks=[
                {
                    "id": "research",
                    "title": "research",
                    "instruction": "collect the evidence",
                    "acceptance": ["evidence"],
                    "depends_on": [],
                    "assignee": "hicode",
                }
            ],
            mode="act_eager",
            wait=True,
            knowledge_plan=RetrievalPlan("canonical question", ["claim"]),
            retriever=retrieve,
            provider_router=Router(),
            provider_request=provider,
            provider_candidates=["provider-a", "provider-b"],
        )
    finally:
        reset_event_capability(capability_token)

    assert result.goal_id
    assert ("retrieve", "canonical question", "web", 1) in calls
    assert calls[-2:] == [("provider", "provider-a"), ("provider", "provider-b")]


@pytest.mark.asyncio
async def test_master_bound_llm_uses_reliable_adapter_for_canonical_execution():
    calls = []

    async def llm(messages, **kwargs):
        calls.append((kwargs["provider"], messages, kwargs["tools"]))
        if kwargs["provider"] == "provider-a":
            raise TimeoutError("controlled provider timeout")
        return {"choices": [{"message": {"tool_calls": [{"function": {"name": "next"}}]}}]}

    class Router:
        def select(self, candidates, requirements):
            return candidates

    coordinator = MasterCoordinator(
        llm_fn=llm,
        provider="provider-a",
        reliable_provider_adapter=ReliableProviderAdapter(Router()),
    )
    token = _CANONICAL_TASK_CTX.set("goal-run-canonical")
    capability_token = _CAPABILITY_CTX.set("coding")
    try:
        response = await coordinator._bound_llm(
            [{"role": "user", "content": "continue"}],
            tools=[{"type": "function", "function": {"name": "next"}}],
            config={"fallback_providers": ["provider-b"]},
        )
    finally:
        _CAPABILITY_CTX.reset(capability_token)
        _CANONICAL_TASK_CTX.reset(token)

    assert response["choices"][0]["message"]["tool_calls"]
    assert [item[0] for item in calls] == ["provider-a", "provider-b"]
    assert calls[0][2] == calls[1][2]
