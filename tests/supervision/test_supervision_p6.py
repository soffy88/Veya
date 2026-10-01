"""P6: Jev wired into the loop + canonical runner delegation."""

from __future__ import annotations

import types
from pathlib import Path

from veya.decision import JevAnswer, JevDecision, QuestionKind
from veya.supervision import MissionStore, SupervisionRouter
from veya.supervision.loop import MissionLoop
from veya.supervision.runner import canonical_runner, runner_with


def _state():
    node = types.SimpleNamespace(
        title="t",
        assignee="opencode",
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


async def _runner(mission):
    return _state()


class FakeJev:
    def __init__(self, confidence: float) -> None:
        self.confidence = confidence
        self.calls = 0

    async def decide(self, state, questions):
        self.calls += 1
        assert len(questions) == 6  # one fan-out per iteration
        return JevDecision(
            answers={
                "safe_to_continue": JevAnswer(
                    "safe_to_continue",
                    QuestionKind.choice,
                    choice="yes",
                    confidence=self.confidence,
                )
            }
        )


def _loop(tmp_path: Path, jev, mode: str):
    store = MissionStore(tmp_path)
    loop = MissionLoop(store=store, router=SupervisionRouter(store), runner=_runner, jev=jev)
    mid = loop.facade().create(goal="g", supervision_mode=mode, workspace=str(tmp_path)).mission_id
    return loop, store, mid


async def test_jev_decisions_are_attached_to_the_report(tmp_path: Path) -> None:
    jev = FakeJev(0.9)
    loop, store, mid = _loop(tmp_path, jev, "internal")
    await loop.step(mid)
    report = store.get_report(mid, 0)
    assert jev.calls == 1
    assert report is not None and report.jev_decisions
    assert any(e["topic"] == "JEV_DECISION" for e in store.events(mid))


async def test_low_confidence_jev_escalates_auto_to_external(tmp_path: Path) -> None:
    loop, _store, mid = _loop(tmp_path, FakeJev(0.05), "auto")
    out = await loop.step(mid)
    assert out["status"] == "WAITING_EXTERNAL_SUPERVISOR"
    assert out["lineage"] and out["lineage"][0]["trigger"] == "jev_low_confidence_critical"


async def test_jev_failure_never_breaks_the_loop(tmp_path: Path) -> None:
    class Broken:
        async def decide(self, state, questions):
            raise RuntimeError("provider down")

    loop, _store, mid = _loop(tmp_path, Broken(), "internal")
    out = await loop.step(mid)
    assert out["status"] in {"REVIEWING", "ACCEPTED", "DONE", "BLOCKED"}


async def test_canonical_runner_delegates_to_single_dispatch() -> None:
    calls: list[tuple[str, str]] = []

    async def dispatch(root: str, request: str) -> str:
        calls.append((root, request))
        return "executed ok"

    mission = types.SimpleNamespace(workspace="/repo", goal="do it")
    out = await canonical_runner(mission, dispatch=dispatch)
    assert calls == [("/repo", "do it")]
    assert out.final_summary == "executed ok" and out.status == "executed"

    wrapped = runner_with(dispatch)
    assert (await wrapped(mission)).final_summary == "executed ok"
