"""Build a failure-first analysis for the Personal Agent Gold run.

The analysis is deliberately evidence-only.  It joins the deterministic Gold
failure corpus with the immutable scenario fixtures and does not alter labels,
metrics, or replay decisions.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT_CAUSES: dict[str, tuple[str, str, str]] = {
    "mem-irrelevant_memory-001": (
        "filter bug",
        "high",
        "An active memory was accepted without task relevance filtering.",
    ),
    "mem-contradiction-001": (
        "status bug",
        "high",
        "A superseded conflict loser entered the retrieval and usable sets.",
    ),
    "mem-stale-001": (
        "status bug",
        "high",
        "A superseded record was treated as retrievable and usable.",
    ),
    "mem-workspace_isolation-001": (
        "scope bug",
        "high",
        "A workspace-B record crossed into a workspace-A retrieval.",
    ),
    "mem-user_workspace_precedence-001": (
        "precedence bug",
        "high",
        "The workspace-specific rule did not outrank the conflicting superseded user rule.",
    ),
    "skill-wrong_workspace-001": (
        "scope bug",
        "high",
        "A trusted skill from another workspace was eligible for activation.",
    ),
    "skill-should_not_activate-001": (
        "gate bug",
        "high",
        "A semantically similar skill was auto-activated without a deterministic threshold or margin gate.",
    ),
    "skill-version_selection-001": (
        "version bug",
        "high",
        "The newest candidate version was selected instead of the active verified version.",
    ),
    "skill-version_selection-002": (
        "runtime execution",
        "medium",
        "Version selection matched the expected version, but the recorded reuse outcome failed; the fixture has no lower-level runtime evidence.",
    ),
    "skill-multiple_candidates-001": (
        "ranking bug",
        "high",
        "A competing candidate was selected when the expected skill was present in the same scope.",
    ),
    "cont-backend_crash-001": (
        "projection bug",
        "high",
        "The recovered task and artifact were present, but decisions and pending questions were not restored into the continuity projection.",
    ),
    "cont-multiple_tasks-001": (
        "ranking bug",
        "high",
        "A non-target task won continuation despite an unfinished task in the same workspace.",
    ),
    "learning-critical_regression-001": (
        "gate bug",
        "high",
        "The apply path allowed an overall-positive candidate despite a critical scenario regression.",
    ),
}


def _load_jsonl(root: Path) -> dict[str, dict[str, Any]]:
    scenarios: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.jsonl")):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                item = json.loads(line)
                scenario_id = item["scenario_id"]
                if scenario_id in scenarios:
                    raise ValueError(f"duplicate scenario {scenario_id} at {path}:{line_no}")
                scenarios[scenario_id] = item
    return scenarios


def _load_failures(root: Path, eval_run_id: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for path in sorted((root / "failures").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if item.get("eval_run_id") == eval_run_id:
                    failures.append(item)
    return failures


def _record_view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "content": record.get("content"),
        "status": record.get("status"),
        "scope_type": record.get("scope_type"),
        "scope_id": record.get("scope_id"),
        "memory_type": record.get("memory_type"),
        "confidence": record.get("confidence"),
        "source_event_ids": record.get("source_event_ids", []),
        "source_session_ids": record.get("source_session_ids", []),
        "source_task_ids": record.get("source_task_ids", []),
        "supersedes": record.get("supersedes"),
        "superseded_by": record.get("superseded_by"),
    }


def _build_entry(failure: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    scenario_id = failure["scenario_id"]
    root_cause, confidence, evidence = ROOT_CAUSES.get(
        scenario_id,
        ("unknown", "low", "No explicit root-cause mapping exists; requires investigation."),
    )
    actual = failure.get("actual", {})
    memory_actual = actual.get("memory", {})
    skill_actual = actual.get("skill", {})
    continuity_actual = actual.get("continuity", {})
    learning_actual = actual.get("learning", {})
    records = [_record_view(record) for record in scenario.get("gold_memories", [])]
    return {
        "scenario_id": scenario_id,
        "domain": failure.get("domain"),
        "category": scenario.get("category"),
        "difficulty": scenario.get("difficulty"),
        "workspace_id": scenario.get("workspace_id"),
        "root_cause": root_cause,
        "root_cause_confidence": confidence,
        "root_cause_evidence": evidence,
        "reasons": failure.get("reasons", []),
        "expected": failure.get("expected", {}),
        "actual": actual,
        "decision_path": {
            "mode": "deterministic_replay_contract",
            "sessions": scenario.get("sessions", []),
            "replay_trace": scenario.get("replay_trace", {}),
            "note": "The trace is an observable contract fixture; no LLM judge was used.",
        },
        "retrieved_memories": memory_actual.get("retrieved_memory_ids", []),
        "used_memories": memory_actual.get("used_memory_ids", []),
        "memory_records": records,
        "memory_status": {str(row.get("id")): row.get("status") for row in records},
        "memory_scope": {
            str(row.get("id")): {
                "scope_type": row.get("scope_type"),
                "scope_id": row.get("scope_id"),
            }
            for row in records
        },
        "memory_confidence": {str(row.get("id")): row.get("confidence") for row in records},
        "conflict_chain": {
            "active": scenario.get("gold_active_memories", []),
            "superseded": scenario.get("gold_superseded_memories", []),
            "expected_retrieval": scenario.get("gold_expected_retrieval", []),
            "expected_used": scenario.get("gold_expected_used", []),
            "resolution_correct": memory_actual.get("conflict_resolution_correct"),
        },
        "candidate_skills": failure.get("skill_candidates", scenario.get("gold_skills", [])),
        "selected_skill": failure.get(
            "selected_skill", skill_actual.get("activated_skill_ids", [])
        ),
        "skill_version": {
            "selected": skill_actual.get("selected_version"),
            "expected": skill_actual.get("expected_version"),
            "regression_opportunity": skill_actual.get("regression_opportunity"),
            "regression_occurred": skill_actual.get("regression_occurred"),
        },
        "continuity_candidates": {
            "initial_state": scenario.get("initial_state", {}),
            "actual_snapshot": continuity_actual,
            "gold_target": scenario.get("gold_continuity_target", {}),
        },
        "selected_continuity_target": continuity_actual.get("selected_task_id"),
        "learning_candidate": {
            "expected_behavior": scenario.get("expected_learning_behavior", {}),
            "actual_decision": learning_actual.get("decision"),
            "critical_regression": learning_actual.get("critical_regression"),
            "regression_escaped": learning_actual.get("regression_escaped"),
        },
        "baseline_eval": {
            "available": False,
            "value": None,
            "note": "This Gold fixture records decision outcomes, not fabricated score data.",
        },
        "candidate_eval": {
            "available": False,
            "value": None,
            "note": "No candidate score is present in the approved fixture.",
        },
    }


def _report_for_eval(root: Path, eval_run_id: str) -> dict[str, Any]:
    for path in sorted((root / "results").glob("*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if report.get("eval_run_id") == eval_run_id:
            return report
    raise ValueError(f"missing report for eval run {eval_run_id}")


def build_analysis(root: Path, *, eval_run_id: str | None = None) -> dict[str, Any]:
    latest_report = json.loads((root / "results" / "latest.json").read_text(encoding="utf-8"))
    if eval_run_id is None and latest_report.get("failure_count", 0):
        eval_run_id = latest_report.get("eval_run_id")
    if eval_run_id is None:
        # A PASS latest report has an empty failure corpus.  Keep the latest
        # failure-first analysis reproducible by selecting the newest report
        # with a non-empty matching corpus, without changing that report.
        candidates: list[tuple[float, str]] = []
        for path in (root / "failures").glob("*.jsonl"):
            try:
                first = next(
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            except (OSError, StopIteration, json.JSONDecodeError):
                continue
            if first.get("eval_run_id"):
                candidates.append((path.stat().st_mtime, str(first["eval_run_id"])))
        eval_run_id = latest_report.get("eval_run_id") if not candidates else max(candidates)[1]
    report = _report_for_eval(root, str(eval_run_id))
    scenarios = _load_jsonl(root)
    failures = _load_failures(root, report["eval_run_id"])
    if len(failures) != report.get("failure_count"):
        raise ValueError(
            f"failure corpus mismatch: {len(failures)} != {report.get('failure_count')}"
        )
    entries = []
    for failure in sorted(failures, key=lambda item: item["scenario_id"]):
        scenario = scenarios.get(failure["scenario_id"])
        if scenario is None:
            raise ValueError(f"missing scenario fixture for {failure['scenario_id']}")
        entries.append(_build_entry(failure, scenario))
    root_causes = Counter(entry["root_cause"] for entry in entries)
    return {
        "analysis_id": f"failure-analysis-{report['eval_run_id']}",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset_version": report.get("dataset_version"),
        "eval_run_id": report.get("eval_run_id"),
        "git_sha": report.get("git_sha"),
        "approved_count": report.get("approved_count"),
        "failure_count": len(entries),
        "root_cause_counts": dict(sorted(root_causes.items())),
        "evidence_policy": {
            "gold_labels_modified": False,
            "llm_judge_used": False,
            "unknown_scores_fabricated": False,
        },
        "failures": entries,
    }


def render_markdown(analysis: dict[str, Any]) -> str:
    lines = [
        "# Personal Agent Gold Failure Analysis",
        "",
        f"- Analysis: `{analysis['analysis_id']}`",
        f"- Dataset: `{analysis['dataset_version']}`",
        f"- Eval run: `{analysis['eval_run_id']}`",
        f"- Git SHA: `{analysis['git_sha']}`",
        f"- Failures analyzed: `{analysis['failure_count']}`",
        "- Gold labels modified: **no**",
        "- LLM judge used: **no**",
        "",
        "## Root-cause counts",
        "",
        "| Root cause | Count |",
        "|---|---:|",
    ]
    for name, count in analysis["root_cause_counts"].items():
        lines.append(f"| `{name}` | {count} |")
    lines.extend(["", "## Failure details", ""])
    for entry in analysis["failures"]:
        lines.extend(
            [
                f"### `{entry['scenario_id']}`",
                "",
                f"- Category: `{entry['category']}`; difficulty: `{entry['difficulty']}`; domain: `{entry['domain']}`",
                f"- Root cause: **{entry['root_cause']}** ({entry['root_cause_confidence']})",
                f"- Evidence: {entry['root_cause_evidence']}",
                f"- Reasons: `{', '.join(entry['reasons'])}`",
                f"- Retrieved memories: `{json.dumps(entry['retrieved_memories'], ensure_ascii=False)}`",
                f"- Used memories: `{json.dumps(entry['used_memories'], ensure_ascii=False)}`",
                f"- Memory status/scope/confidence: `{json.dumps({'status': entry['memory_status'], 'scope': entry['memory_scope'], 'confidence': entry['memory_confidence']}, ensure_ascii=False, sort_keys=True)}`",
                f"- Conflict chain: `{json.dumps(entry['conflict_chain'], ensure_ascii=False, sort_keys=True)}`",
                f"- Skill candidates: `{json.dumps(entry['candidate_skills'], ensure_ascii=False, sort_keys=True)}`",
                f"- Selected skill/version: `{json.dumps({'skill': entry['selected_skill'], 'version': entry['skill_version']}, ensure_ascii=False, sort_keys=True)}`",
                f"- Continuity candidates/target: `{json.dumps({'candidates': entry['continuity_candidates'], 'selected': entry['selected_continuity_target']}, ensure_ascii=False, sort_keys=True)}`",
                f"- Learning candidate/evals: `{json.dumps({'candidate': entry['learning_candidate'], 'baseline': entry['baseline_eval'], 'candidate_eval': entry['candidate_eval']}, ensure_ascii=False, sort_keys=True)}`",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--eval-run-id")
    args = parser.parse_args()
    analysis = build_analysis(args.root, eval_run_id=args.eval_run_id)
    results = args.root / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "failure-analysis-latest.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (results / "failure-analysis-latest.md").write_text(render_markdown(analysis), encoding="utf-8")
    print(
        json.dumps(
            {
                "failure_count": len(analysis["failures"]),
                "root_cause_counts": analysis["root_cause_counts"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
