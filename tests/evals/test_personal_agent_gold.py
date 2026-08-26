"""Regression tests for the manually labelled Personal Agent benchmark."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.personal_agent_gold.benchmark import (
    _memory_metrics,
    _wilson,
    load_dataset,
    record_shadow_candidate,
    run_benchmark,
)
from runtime.personal.runtime import PersonalRuntimeStore

ROOT = Path(__file__).parents[2] / "evals" / "personal_agent_gold"


def test_gold_dataset_has_required_size_and_review_status():
    manifest, scenarios = load_dataset(ROOT)

    assert manifest["dataset_version"] == "personal-agent-gold-v1"
    assert len(scenarios) == 170
    assert all(scenario["review_status"] == "approved" for scenario in scenarios)
    assert all(scenario["reviewer_type"] == "human" for scenario in scenarios)
    assert {scenario["category"] for scenario in scenarios}


def test_benchmark_denominators_are_domain_specific():
    report = run_benchmark(ROOT, git_sha="test", write_outputs=False)

    assert report["metrics"]["continuity_task_recovery_accuracy"]["denominator"] == 30
    assert report["metrics"]["learning_candidate_precision"]["denominator"] == 30
    assert report["metrics"]["wrong_skill_activation_rate"]["denominator"] == 50
    assert report["metrics"]["memory_correction_success_rate"]["denominator"] == 8

    difficulty = report["slices"]["difficulty"]
    assert sum(item["scenario_count"] for item in difficulty.values()) == 170
    continuity_den = sum(
        item["metrics"]["continuity_task_recovery_accuracy"]["denominator"]
        for item in difficulty.values()
    )
    assert continuity_den == 30


def test_memory_precision_counts_only_actual_usage():
    scenarios = [
        {
            "scenario_id": "one",
            "category": "stable_preference",
            "gold_expected_retrieval": ["m1"],
            "gold_expected_used": ["m1"],
            "gold_forbidden_used": [],
            "gold_non_memories": [],
            "gold_superseded_memories": [],
            "replay_trace": {
                "memory": {
                    "retrieved_memory_ids": ["m1"],
                    "used_memory_ids": ["m1"],
                }
            },
        },
        {
            "scenario_id": "two",
            "category": "stable_preference",
            "gold_expected_retrieval": ["m2"],
            "gold_expected_used": ["m2"],
            "gold_forbidden_used": [],
            "gold_non_memories": [],
            "gold_superseded_memories": [],
            "replay_trace": {
                "memory": {
                    "retrieved_memory_ids": ["m2"],
                    "used_memory_ids": ["m2", "wrong"],
                }
            },
        },
    ]

    metrics, _ = _memory_metrics(scenarios)

    assert metrics["memory_precision"].numerator == 2
    assert metrics["memory_precision"].denominator == 3


def test_ci_is_wilson_and_reports_zero_denominator_as_null():
    low, high = _wilson(47, 50)

    assert 0.8 < low < 0.9
    assert 0.9 < high < 1.0
    assert _wilson(0, 0) is None


def test_unapproved_scenario_is_excluded(tmp_path):
    _, scenarios = load_dataset(ROOT)
    approved = dict(scenarios[0])
    draft = dict(scenarios[1])
    draft["scenario_id"] = "draft-only"
    draft["review_status"] = "draft"
    path = tmp_path / "scenarios.jsonl"
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in (approved, draft)) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "dataset_version": "personal-agent-gold-v1",
                "schema_version": 1,
                "label_version": "human-gold-v1",
                "scenario_files": ["scenarios.jsonl"],
                "scenario_count": 2,
                "approved_count": 1,
            }
        ),
        encoding="utf-8",
    )

    manifest, loaded = load_dataset(tmp_path)

    assert manifest["scenario_count"] == 2
    assert [scenario["scenario_id"] for scenario in loaded] == [approved["scenario_id"]]


def test_dataset_hash_guard_rejects_mutation(tmp_path):
    for source in ROOT.iterdir():
        if source.is_file() and source.name != "manifest.json":
            (tmp_path / source.name).write_bytes(source.read_bytes())
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with (tmp_path / "memory.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("\n")

    with pytest.raises(ValueError, match="immutable dataset hash mismatch"):
        load_dataset(tmp_path)


def test_shadow_candidate_is_privacy_safe_and_draft_only(tmp_path):
    candidate = record_shadow_candidate(
        tmp_path,
        category="memory_correction",
        source_event_id="event-1",
        source_task_id="task-1",
        outcome="private user answer must not be persisted",
        correction=True,
    )
    raw = (tmp_path / "candidate_eval_cases.jsonl").read_text(encoding="utf-8")

    assert candidate["review_status"] == "draft"
    assert "private user answer" not in raw
    assert "event-1" not in raw
    assert "task-1" not in raw
    assert "outcome_hash" in raw


@pytest.mark.asyncio
async def test_personal_metrics_exposes_approved_report_without_changing_search(tmp_path):
    store = PersonalRuntimeStore(sqlite_path=tmp_path / "personal.db", production=False)
    await store.connect()
    try:
        metrics = await store.personal_metrics()

        assert metrics["gold_benchmark"]["dataset_version"] == "personal-agent-gold-v1"
        assert metrics["memory_precision"] == metrics["gold_benchmark"]["metrics"]["memory_precision"]["rate"]
        assert isinstance(await store.search_memory("not present"), list)
    finally:
        await store.close()
