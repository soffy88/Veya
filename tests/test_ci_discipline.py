"""Tests for the CI discipline: gated review, merge gate, checks, version."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.events import EventStore
from server.goal_run import code_review
from server.goal_run.merge_gate import merge_gate_passed
from server.goal_run.review_gate import (
    MAX_REVIEW_ROUNDS,
    classify,
    diff_hash_of,
    review_passed,
    run_gated_review,
)
from server.goal_run.sandbox_checks import (
    ChecksResult,
    CommandResult,
    record_checks,
    run_checks,
    validate_commands,
)
from server.version_info import read_deployed_commit

# ── severity parsing (additive; old behavior unchanged) ────────────────────


def _llm_response(payload: dict):
    async def call(messages):
        return {"choices": [{"message": {"content": json.dumps(payload)}}]}

    return call


async def test_severity_normalizes_missing_and_bad_values():
    payload = {
        "findings": [
            {"rule": "r1", "detail": "d1"},
            {"rule": "r2", "detail": "d2", "severity": "blocker"},
            {"rule": "r3", "detail": "d3", "severity": "weird"},
            "plain string finding",
        ],
        "worst": "r2",
    }
    result = await code_review.dual_axis_review(
        diff_text="diff",
        task_instruction="do it",
        acceptance=["a"],
        llm_call_fn=_llm_response(payload),
        severity=True,
    )
    severities = [f["severity"] for f in result["standards"]["findings"]]
    assert severities == ["major", "blocker", "major", "major"]


async def test_severity_off_leaves_findings_untouched():
    payload = {"findings": [{"rule": "r1", "detail": "d1", "severity": "blocker"}], "worst": None}
    result = await code_review.dual_axis_review(
        diff_text="diff",
        task_instruction="do it",
        acceptance=["a"],
        llm_call_fn=_llm_response(payload),
    )
    assert result["standards"]["findings"] == payload["findings"]


async def test_unscanned_axes_are_inconclusive():
    async def stub(messages):
        return {"choices": [{"message": {"content": "LLM provider not configured"}}]}

    result = await code_review.dual_axis_review(
        diff_text="diff",
        task_instruction="do it",
        acceptance=["a"],
        llm_call_fn=stub,
        severity=True,
    )
    passed, reason = review_passed(result)
    assert passed is False
    assert "unscanned" in reason


def test_classify_and_pass():
    dual = {
        "standards": {"findings": [{"severity": "nit", "detail": "x"}]},
        "spec": {"findings": [{"severity": "major", "detail": "y"}]},
    }
    assert classify(dual["standards"])["nit"]
    assert review_passed(dual)[0] is True
    dual["spec"]["findings"].append({"severity": "blocker", "detail": "z"})
    assert review_passed(dual)[0] is False


# ── gated rounds ───────────────────────────────────────────────────────────


def _clean_llm(payload: dict):
    return _llm_response(payload)


async def test_unchanged_diff_reuses_round_without_model_call(tmp_path):
    events = EventStore(path=str(tmp_path / "events.jsonl"))
    calls = {"n": 0}

    async def counting(messages):
        calls["n"] += 1
        return {"choices": [{"message": {"content": json.dumps({"findings": [], "worst": None})}}]}

    first = await run_gated_review(
        diff_text="diff-v1",
        task_instruction="do it",
        acceptance=["a"],
        standards_doc="",
        subject_id="subj-1",
        events=events,
        llm_call_fn=counting,
    )
    second = await run_gated_review(
        diff_text="diff-v1",
        task_instruction="do it",
        acceptance=["a"],
        standards_doc="",
        subject_id="subj-1",
        events=events,
        llm_call_fn=counting,
    )
    assert first["round"] == 1 and first["passed"] is True
    assert second["reused"] is True and second["round"] == 1
    assert calls["n"] == 2  # two axes, one round only


async def test_changed_diff_advances_round(tmp_path):
    events = EventStore(path=str(tmp_path / "events.jsonl"))
    clean = _clean_llm({"findings": [], "worst": None})
    first = await run_gated_review(
        diff_text="diff-v1",
        task_instruction="do it",
        acceptance=["a"],
        standards_doc="",
        subject_id="subj-1",
        events=events,
        llm_call_fn=clean,
    )
    second = await run_gated_review(
        diff_text="diff-v2",
        task_instruction="do it",
        acceptance=["a"],
        standards_doc="",
        subject_id="subj-1",
        events=events,
        llm_call_fn=clean,
    )
    assert (first["round"], second["round"]) == (1, 2)


async def test_round_cap_raises_incident_without_model_call(tmp_path):
    events = EventStore(path=str(tmp_path / "events.jsonl"))
    blocker = _clean_llm(
        {"findings": [{"rule": "r", "detail": "bad", "severity": "blocker"}], "worst": "bad"}
    )
    incidents: list = []

    def record_incident(subject_id, payload):
        incidents.append((subject_id, payload))

    results = []
    for version in range(MAX_REVIEW_ROUNDS + 1):
        results.append(
            await run_gated_review(
                diff_text=f"diff-v{version}",
                task_instruction="do it",
                acceptance=["a"],
                standards_doc="",
                subject_id="subj-1",
                events=events,
                llm_call_fn=blocker,
                incident_fn=record_incident,
            )
        )
    assert [r["round"] for r in results] == [1, 2, 3, 4]
    assert results[3]["capped"] is True
    assert len(incidents) == 1
    assert incidents[0][1]["round"] == 4
    capped_events = events.read_all(task_id="subj-1", topics={"review.capped"})
    assert len(capped_events) == 1


# ── merge gate ─────────────────────────────────────────────────────────────


def _seed(events, subject, *, review_passed_flag, checks_passed_flag, review_hash, checks_hash):
    if review_passed_flag is not None:
        events.append(
            {
                "topic": "review.completed",
                "task_id": subject,
                "actor": "system",
                "payload": {
                    "subject_id": subject,
                    "diff_hash": review_hash,
                    "passed": review_passed_flag,
                },
            }
        )
    if checks_passed_flag is not None:
        events.append(
            {
                "topic": "checks.completed",
                "task_id": subject,
                "actor": "system",
                "payload": {
                    "subject_id": subject,
                    "diff_hash": checks_hash,
                    "passed": checks_passed_flag,
                },
            }
        )


def test_merge_gate_variants(tmp_path):
    digest = diff_hash_of("some diff")
    cases = [
        (True, True, digest, digest, True),
        (True, True, digest, "other", False),
        (False, True, digest, digest, False),
        (True, False, digest, digest, False),
        (None, True, digest, digest, False),
        (True, None, digest, digest, False),
    ]
    for i, (rp, cp, rh, ch, want) in enumerate(cases):
        events = EventStore(path=str(tmp_path / f"events-{i}.jsonl"))
        _seed(events, "subj", review_passed_flag=rp, checks_passed_flag=cp, review_hash=rh, checks_hash=ch)
        passed, _reason = merge_gate_passed("subj", events=events)
        assert passed is want, f"case {i}"
    # Latest record wins over an older pass.
    events = EventStore(path=str(tmp_path / "events-latest.jsonl"))
    _seed(events, "subj", review_passed_flag=True, checks_passed_flag=True, review_hash=digest, checks_hash=digest)
    _seed(events, "subj", review_passed_flag=False, checks_passed_flag=None, review_hash=digest, checks_hash=digest)
    assert merge_gate_passed("subj", events=events)[0] is False


def test_merge_gate_reads_record_checks_shape(tmp_path):
    events = EventStore(path=str(tmp_path / "events.jsonl"))
    digest = diff_hash_of("d")
    events.append(
        {
            "topic": "review.completed",
            "task_id": "s",
            "actor": "system",
            "payload": {"diff_hash": digest, "passed": True},
        }
    )
    record_checks(
        events,
        "s",
        digest,
        ChecksResult(passed=True, commands=[], reason="all checks passed"),
    )
    assert merge_gate_passed("s", events=events)[0] is True


# ── checks runner ──────────────────────────────────────────────────────────


def test_validate_commands_rejects_bad_shapes():
    with pytest.raises(ValueError):
        validate_commands([])
    with pytest.raises(ValueError):
        validate_commands([["/bin/rm", "-rf"]])
    with pytest.raises(ValueError):
        validate_commands([["pytest", "a\nb"]])
    with pytest.raises(ValueError):
        validate_commands([["pytest;rm"]])
    assert validate_commands([["pytest", "-q"], ["ruff", "check"]])


async def test_run_checks_fake_executor(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.py").write_text("x = 1\n")
    seen: list = []

    async def fake(argv, cwd, timeout_s):
        seen.append((argv, cwd))
        assert Path(cwd, "a.py").exists()
        assert ".git" not in str(cwd)
        return CommandResult(argv=argv, exit_code=0, stdout="ok", stderr="", duration_s=0.01)

    result = await run_checks(tree=str(tree), commands=[["pytest", "-q"]], executor=fake)
    assert result.passed is True
    assert len(seen) == 1


async def test_run_checks_stops_at_first_failure(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()

    async def fake(argv, cwd, timeout_s):
        code = 0 if argv[0] == "ruff" else 1
        return CommandResult(argv=argv, exit_code=code, stdout="", stderr="boom", duration_s=0.01)

    result = await run_checks(
        tree=str(tree), commands=[["ruff"], ["pytest"]], executor=fake
    )
    assert result.passed is False
    assert len(result.commands) == 2
    assert "pytest" in result.reason


async def test_run_checks_real_subprocess(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "hello.txt").write_text("hi\n")
    result = await run_checks(
        tree=str(tree), commands=[["ls", "hello.txt"]], timeout_s=30
    )
    assert result.passed is True
    assert "hello.txt" in result.commands[0].stdout


async def test_run_checks_timeout_kills(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    result = await run_checks(
        tree=str(tree),
        commands=[["sleep", "30"]],
        timeout_s=1,
    )
    assert result.passed is False
    assert result.commands[0].timed_out is True


# ── version info ───────────────────────────────────────────────────────────


def test_read_deployed_commit_variants(tmp_path, monkeypatch):
    good = "a" * 40
    commit_file = tmp_path / "commit.json"
    commit_file.write_text(json.dumps({"commit": good, "written_at": "2026-11-04T00:00:00Z"}))
    monkeypatch.setenv("VEYA_COMMIT_FILE", str(commit_file))
    monkeypatch.delenv("VEYA_BUILD_SHA", raising=False)
    info = read_deployed_commit()
    assert info == {"commit": good, "source": "commit-file", "written_at": "2026-11-04T00:00:00Z"}
    commit_file.write_text(json.dumps({"commit": "not-a-sha"}))
    monkeypatch.setenv("VEYA_BUILD_SHA", good)
    assert read_deployed_commit()["source"] == "env"
    commit_file.unlink()
    monkeypatch.delenv("VEYA_BUILD_SHA", raising=False)
    assert read_deployed_commit() == {"commit": None, "source": "unknown", "written_at": ""}
