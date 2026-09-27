"""P5: the single durable loop — restart-safe, resume, internal autonomous, auto switch."""

from __future__ import annotations

import types
from pathlib import Path

from veya.supervision import InternalSupervisor, MissionStore, SupervisionRouter
from veya.supervision.loop import MissionLoop


def _clean_state():
    node = types.SimpleNamespace(
        title="t",
        assignee="hicode",
        status="completed",
        acceptance=["ok"],
        verify_summary="passed",
        artifacts=[],
        evidence=[],
        retries=0,
        block_reason=None,
        unfinished_work=[],
    )
    return types.SimpleNamespace(
        goal_id="gr", status="completed", tasks={"t": node}, final_summary="ok", unfinished_work=[]
    )


class CountingRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, mission):
        self.calls += 1
        return _clean_state()


def _llm_sequence(responses: list[str]):
    queue = list(responses)

    async def llm(_: str) -> str:
        return queue.pop(0) if queue else responses[-1]

    return llm


def _loop(tmp_path: Path, *, runner, llm=None, mode="external"):
    store = MissionStore(tmp_path)
    router = SupervisionRouter(store)
    loop = MissionLoop(
        store=store,
        router=router,
        runner=runner,
        internal=InternalSupervisor(llm=llm) if llm else None,
    )
    mission = loop.facade().create(goal="g", supervision_mode=mode, workspace=str(tmp_path))
    return loop, store, mission.mission_id


async def test_restart_does_not_re_execute_the_same_iteration(tmp_path: Path) -> None:
    runner = CountingRunner()
    loop, store, mid = _loop(tmp_path, runner=runner)
    await loop.step(mid)  # executes iteration 0, parks for external
    assert runner.calls == 1
    # simulate a process restart: fresh loop, same store
    loop2 = MissionLoop(store=store, router=SupervisionRouter(store), runner=runner)
    await loop2.step(mid)  # report already persisted -> no duplicate mutation
    assert runner.calls == 1
    assert len(store.reports(mid)) == 1


async def test_internal_mode_finishes_autonomously(tmp_path: Path) -> None:
    runner = CountingRunner()
    loop, store, mid = _loop(
        tmp_path,
        runner=runner,
        llm=_llm_sequence(['{"decision": "ACCEPT", "reason": "evidence ok"}']),
        mode="internal",
    )
    out = await loop.run_to_completion(mid)
    assert out["status"] == "ACCEPTED"
    assert runner.calls == 1
    topics = [e["topic"] for e in store.events(mid)]
    assert "REVIEW_COMPLETED" in topics


async def test_internal_revise_retasks_and_continues(tmp_path: Path) -> None:
    runner = CountingRunner()
    loop, store, mid = _loop(
        tmp_path,
        runner=runner,
        llm=_llm_sequence(
            [
                '{"decision": "REVISE", "reason": "missing", "next_task": "add test"}',
                '{"decision": "ACCEPT", "reason": "complete"}',
            ]
        ),
        mode="internal",
    )
    out = await loop.run_to_completion(mid)
    assert out["status"] == "ACCEPTED"
    assert runner.calls == 2  # iteration 0 + retasked iteration 1
    assert any(e["topic"] == "RETASK_CREATED" for e in store.events(mid))


async def test_external_reconnect_resumes_after_review_apply(tmp_path: Path) -> None:
    runner = CountingRunner()
    loop, _store, mid = _loop(tmp_path, runner=runner, mode="external")
    first = await loop.step(mid)
    assert first["status"] == "WAITING_EXTERNAL_SUPERVISOR"
    # still parked until a review for this iteration exists
    assert (await loop.resume(mid))["reason"] == "still_waiting"
    # external supervisor reconnects and applies a review
    loop.facade().apply_review(mid, {"decision": "RETRY", "next_task": "do it again"})
    resumed = await loop.resume(mid)
    assert runner.calls == 2  # next iteration executed after the retask
    assert resumed["iteration"] == 1


async def test_auto_low_confidence_switches_to_external(tmp_path: Path) -> None:
    runner = CountingRunner()
    loop, _store, mid = _loop(
        tmp_path,
        runner=runner,
        llm=_llm_sequence(['{"decision": "ACCEPT", "reason": "unsure", "confidence": 0.1}']),
        mode="auto",
    )
    out = await loop.step(mid)
    assert out["status"] == "WAITING_EXTERNAL_SUPERVISOR"
    assert out["lineage"] and out["lineage"][0]["to"] == "external"
