"""Deterministic Personal Agent Gold Benchmark.

This module is intentionally separate from the Personal Runtime.  A scenario
contains a manually authored gold label and a replay trace captured from the
runtime contract.  The evaluator never asks an LLM to create labels or score
answers.  This makes the result auditable while keeping natural-language judge
integration out of the release gate.

The replay trace is not production telemetry.  It is a deterministic fixture
of the observable retrieval/selection/recovery decision that the runtime is
required to make.  Production shadow candidates are stored separately and can
enter this dataset only after review and a dataset version bump.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DATASET_VERSION = "personal-agent-gold-v1"
SCHEMA_VERSION = 1
RUNTIME_SCHEMA_VERSION = 3
LABEL_VERSION = "human-gold-v1"
Z95 = 1.959963984540054

MEMORY_CATEGORIES = frozenset(
    {
        "stable_preference",
        "irrelevant_memory",
        "correction",
        "contradiction",
        "stale",
        "workspace_isolation",
        "user_workspace_precedence",
    }
)
SKILL_CATEGORIES = frozenset(
    {
        "exact_activation",
        "similar_wording",
        "wrong_workspace",
        "should_not_activate",
        "version_selection",
        "deprecated_blocked",
        "multiple_candidates",
    }
)
CONTINUITY_CATEGORIES = frozenset(
    {"cli_web", "web_cli", "interrupted", "backend_crash", "multiple_tasks", "same_workspace_old_tasks"}
)
LEARNING_CATEGORIES = frozenset(
    {"single_failure", "below_threshold", "threshold_reached", "replay_rejected", "improvement_validated", "critical_regression"}
)

TARGETS: dict[str, tuple[str, float, str]] = {
    "retrieval_precision": (">=", 0.95, "memory"),
    "memory_precision": (">=", 0.95, "memory"),
    "memory_recall_when_needed": (">=", 0.90, "memory"),
    "unnecessary_memory_use_rate": ("<=", 0.05, "memory"),
    "stale_memory_use_rate": ("<=", 0.01, "memory"),
    "memory_conflict_resolution_accuracy": (">=", 0.95, "memory"),
    "memory_correction_success_rate": (">=", 0.99, "memory"),
    "skill_activation_precision": (">=", 0.95, "skill"),
    "wrong_skill_activation_rate": ("<=", 0.02, "skill"),
    "skill_reuse_success_rate": (">=", 0.90, "skill"),
    "skill_regression_rate": ("<=", 0.01, "skill"),
    "skill_version_selection_accuracy": (">=", 0.95, "skill"),
    "continuity_task_recovery_accuracy": (">=", 0.98, "continuity"),
    "continuity_state_restore_accuracy": (">=", 0.98, "continuity"),
    "learning_candidate_precision": (">=", 0.95, "learning"),
    "learning_regression_escape_rate": ("<=", 0.0, "learning"),
}

REQUIRED_FIELDS = {
    "scenario_id",
    "category",
    "difficulty",
    "workspace_id",
    "initial_state",
    "sessions",
    "gold_memories",
    "gold_non_memories",
    "gold_active_memories",
    "gold_superseded_memories",
    "gold_expected_retrieval",
    "gold_forbidden_retrieval",
    "gold_expected_used",
    "gold_forbidden_used",
    "gold_skills",
    "gold_expected_skill_activation",
    "gold_forbidden_skill_activation",
    "gold_continuity_target",
    "expected_learning_behavior",
    "labels_version",
    "review_status",
    "replay_trace",
}


@dataclass(frozen=True)
class Metric:
    numerator: int
    denominator: int
    rate: float | None
    ci95: tuple[float, float] | None
    target_operator: str
    target: float
    passed: bool | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
            "rate": self.rate,
            "ci95": None
            if self.ci95 is None
            else {"low": self.ci95[0], "high": self.ci95[1]},
            "target_operator": self.target_operator,
            "target": self.target,
            "passed": self.passed,
        }


def _wilson(successes: int, total: int) -> tuple[float, float] | None:
    if total <= 0:
        return None
    p = successes / total
    z2 = Z95 * Z95
    denominator = 1 + z2 / total
    centre = (p + z2 / (2 * total)) / denominator
    margin = Z95 * ((p * (1 - p) / total + z2 / (4 * total * total)) ** 0.5)
    margin /= denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _metric(name: str, numerator: int, denominator: int) -> Metric:
    operator, target, _ = TARGETS[name]
    rate = numerator / denominator if denominator else None
    passed: bool | None
    if rate is None:
        passed = None
    elif operator == ">=":
        passed = rate >= target
    else:
        passed = rate <= target
    return Metric(numerator, denominator, rate, _wilson(numerator, denominator), operator, target, passed)


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _json_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _failure(
    scenario: dict[str, Any], domain: str, reasons: list[str]
) -> dict[str, Any]:
    """Keep enough structured evidence to reproduce a failure offline."""
    trace = scenario["replay_trace"]
    return {
        "scenario_id": scenario["scenario_id"],
        "domain": domain,
        "reasons": reasons,
        "scenario": {
            "scenario_id": scenario.get("scenario_id"),
            "category": scenario.get("category"),
            "difficulty": scenario.get("difficulty"),
            "workspace_id": scenario.get("workspace_id"),
        },
        "expected": {
            "memories": {
                "retrieval": scenario.get("gold_expected_retrieval", []),
                "used": scenario.get("gold_expected_used", []),
                "forbidden_retrieval": scenario.get("gold_forbidden_retrieval", []),
                "forbidden_used": scenario.get("gold_forbidden_used", []),
            },
            "skills": {
                "activation": scenario.get("gold_expected_skill_activation", []),
                "forbidden_activation": scenario.get("gold_forbidden_skill_activation", []),
            },
            "continuity": scenario.get("gold_continuity_target", {}),
            "learning": scenario.get("expected_learning_behavior", {}),
        },
        "actual": trace,
        "trace": scenario.get("sessions", []),
        "retrieved_memories": trace.get("memory", {}).get("retrieved_memory_ids", []),
        "used_memories": trace.get("memory", {}).get("used_memory_ids", []),
        "skill_candidates": scenario.get("gold_skills", []),
        "selected_skill": trace.get("skill", {}).get("activated_skill_ids", []),
        "continuity_snapshot": trace.get("continuity", {}),
        "learning_decision": trace.get("learning", {}).get("decision"),
    }


def _validate_scenario(raw: dict[str, Any], line_no: int) -> None:
    missing = sorted(REQUIRED_FIELDS - raw.keys())
    if missing:
        raise ValueError(f"scenario line {line_no} missing fields: {', '.join(missing)}")
    if raw["review_status"] not in {"draft", "reviewed", "approved"}:
        raise ValueError(f"scenario {raw['scenario_id']} has invalid review_status")
    if raw["difficulty"] not in {"easy", "medium", "hard"}:
        raise ValueError(f"scenario {raw['scenario_id']} has invalid difficulty")
    if raw["labels_version"] != LABEL_VERSION:
        raise ValueError(f"scenario {raw['scenario_id']} has incompatible labels_version")
    if not isinstance(raw["sessions"], list) or not raw["sessions"]:
        raise ValueError(f"scenario {raw['scenario_id']} must have sessions")
    if (
        raw["review_status"] == "approved"
        and (raw.get("reviewer_type") != "human" or not raw.get("reviewed_by"))
    ):
        raise ValueError(f"approved scenario {raw['scenario_id']} lacks human reviewer")
    if not isinstance(raw["replay_trace"], dict):
        raise ValueError(f"scenario {raw['scenario_id']} replay_trace must be an object")


def load_dataset(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("dataset_version") != DATASET_VERSION:
        raise ValueError("unsupported Gold dataset version")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported Gold scenario schema version")
    scenarios: list[dict[str, Any]] = []
    seen: set[str] = set()
    for relative in manifest.get("scenario_files", []):
        path = root / relative
        if not path.is_file():
            raise ValueError(f"manifest references missing scenario file: {relative}")
        expected_hash = manifest.get("file_hashes", {}).get(relative)
        if expected_hash and expected_hash != _sha256(path):
            raise ValueError(f"immutable dataset hash mismatch: {relative}")
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            _validate_scenario(raw, line_no)
            if raw["scenario_id"] in seen:
                raise ValueError(f"duplicate scenario_id: {raw['scenario_id']}")
            seen.add(raw["scenario_id"])
            scenarios.append(raw)
    if len(scenarios) != manifest.get("scenario_count"):
        raise ValueError("manifest scenario_count does not match dataset")
    approved = [scenario for scenario in scenarios if scenario["review_status"] == "approved"]
    if len(approved) != manifest.get("approved_count"):
        raise ValueError("manifest approved_count does not match dataset")
    return manifest, approved


def _memory_metrics(scenarios: Iterable[dict[str, Any]]) -> tuple[dict[str, Metric], list[dict[str, Any]]]:
    metrics = Counter()
    failures: list[dict[str, Any]] = []
    conflict_categories = {"correction", "contradiction", "user_workspace_precedence"}
    correction_categories = {"correction"}
    unrelated_categories = {"irrelevant_memory", "workspace_isolation"}
    for scenario in scenarios:
        trace = scenario["replay_trace"].get("memory", {})
        expected_retrieval = set(scenario["gold_expected_retrieval"])
        expected_used = set(scenario["gold_expected_used"])
        forbidden_used = set(scenario["gold_forbidden_used"])
        stale = set(scenario["gold_superseded_memories"])
        retrieved = set(trace.get("retrieved_memory_ids", []))
        used = set(trace.get("used_memory_ids", []))
        metrics["retrieval_den"] += len(retrieved)
        metrics["retrieval_num"] += len(retrieved & expected_retrieval)
        metrics["precision_den"] += len(used)
        metrics["precision_num"] += len(used & expected_used)
        metrics["recall_den"] += len(expected_used)
        metrics["recall_num"] += len(used & expected_used)
        if scenario["category"] in unrelated_categories:
            metrics["unnecessary_den"] += 1
            metrics["unnecessary_num"] += bool(used & forbidden_used)
        if stale:
            metrics["stale_den"] += 1
            metrics["stale_num"] += bool(used & stale)
        if scenario["category"] in conflict_categories:
            metrics["conflict_den"] += 1
            metrics["conflict_num"] += bool(trace["conflict_resolution_correct"])
        if scenario["category"] in correction_categories:
            metrics["correction_den"] += 1
            metrics["correction_num"] += bool(trace["correction_success"])

        reasons: list[str] = []
        if retrieved - expected_retrieval:
            reasons.append("forbidden_or_unexpected_retrieval")
        if used - expected_used:
            reasons.append("forbidden_or_unexpected_usage")
        if stale & used:
            reasons.append("stale_memory_used")
        if trace.get("conflict_resolution_correct") is False:
            reasons.append("conflict_resolution_failed")
        if trace.get("correction_success") is False:
            reasons.append("correction_failed")
        if reasons:
            failures.append(_failure(scenario, "memory", reasons))
    return {
        "retrieval_precision": _metric("retrieval_precision", metrics["retrieval_num"], metrics["retrieval_den"]),
        "memory_precision": _metric("memory_precision", metrics["precision_num"], metrics["precision_den"]),
        "memory_recall_when_needed": _metric("memory_recall_when_needed", metrics["recall_num"], metrics["recall_den"]),
        "unnecessary_memory_use_rate": _metric("unnecessary_memory_use_rate", metrics["unnecessary_num"], metrics["unnecessary_den"]),
        "stale_memory_use_rate": _metric("stale_memory_use_rate", metrics["stale_num"], metrics["stale_den"]),
        "memory_conflict_resolution_accuracy": _metric("memory_conflict_resolution_accuracy", metrics["conflict_num"], metrics["conflict_den"]),
        "memory_correction_success_rate": _metric("memory_correction_success_rate", metrics["correction_num"], metrics["correction_den"]),
    }, failures


def _skill_metrics(scenarios: Iterable[dict[str, Any]]) -> tuple[dict[str, Metric], list[dict[str, Any]]]:
    metrics = Counter()
    failures: list[dict[str, Any]] = []
    for scenario in scenarios:
        trace = scenario["replay_trace"].get("skill", {})
        expected = set(scenario["gold_expected_skill_activation"])
        forbidden = set(scenario["gold_forbidden_skill_activation"])
        activated = set(trace.get("activated_skill_ids", []))
        metrics["activation_den"] += len(activated)
        metrics["activation_num"] += len(activated & expected)
        metrics["opportunity_den"] += 1
        metrics["wrong_num"] += bool((activated & forbidden) or (activated - expected))
        if expected:
            metrics["reuse_den"] += 1
            metrics["reuse_num"] += bool(trace.get("reuse_success"))
        if trace.get("regression_opportunity"):
            metrics["regression_den"] += 1
            metrics["regression_num"] += bool(trace.get("regression_occurred"))
        if trace.get("expected_version") is not None:
            metrics["version_den"] += 1
            metrics["version_num"] += trace.get("selected_version") == trace.get("expected_version")

        reasons: list[str] = []
        if (activated & forbidden) or (activated - expected):
            reasons.append("wrong_skill_activation")
        if expected and not trace.get("reuse_success"):
            reasons.append("skill_reuse_failed")
        if trace.get("regression_occurred"):
            reasons.append("skill_regression_occurred")
        if (
            trace.get("expected_version") is not None
            and trace.get("selected_version") != trace.get("expected_version")
        ):
            reasons.append("wrong_skill_version")
        if reasons:
            failures.append(_failure(scenario, "skill", reasons))
    return {
        "skill_activation_precision": _metric("skill_activation_precision", metrics["activation_num"], metrics["activation_den"]),
        "wrong_skill_activation_rate": _metric("wrong_skill_activation_rate", metrics["wrong_num"], metrics["opportunity_den"]),
        "skill_reuse_success_rate": _metric("skill_reuse_success_rate", metrics["reuse_num"], metrics["reuse_den"]),
        "skill_regression_rate": _metric("skill_regression_rate", metrics["regression_num"], metrics["regression_den"]),
        "skill_version_selection_accuracy": _metric("skill_version_selection_accuracy", metrics["version_num"], metrics["version_den"]),
    }, failures


def _continuity_metrics(scenarios: Iterable[dict[str, Any]]) -> tuple[dict[str, Metric], list[dict[str, Any]]]:
    metrics = Counter()
    failures: list[dict[str, Any]] = []
    for scenario in scenarios:
        trace = scenario["replay_trace"].get("continuity", {})
        metrics["task_den"] += 1
        metrics["task_num"] += bool(trace.get("task_recovered"))
        metrics["state_den"] += 1
        metrics["state_num"] += bool(trace.get("state_restored"))
        reasons = []
        if not trace.get("task_recovered"):
            reasons.append("wrong_task_recovery")
        if not trace.get("state_restored"):
            reasons.append("continuity_state_not_restored")
        if reasons:
            failures.append(_failure(scenario, "continuity", reasons))
    return {
        "continuity_task_recovery_accuracy": _metric("continuity_task_recovery_accuracy", metrics["task_num"], metrics["task_den"]),
        "continuity_state_restore_accuracy": _metric("continuity_state_restore_accuracy", metrics["state_num"], metrics["state_den"]),
    }, failures


def _learning_metrics(scenarios: Iterable[dict[str, Any]]) -> tuple[dict[str, Metric], list[dict[str, Any]]]:
    metrics = Counter()
    failures: list[dict[str, Any]] = []
    for scenario in scenarios:
        trace = scenario["replay_trace"].get("learning", {})
        expected = scenario["expected_learning_behavior"].get("decision")
        metrics["candidate_den"] += 1
        metrics["candidate_num"] += trace.get("decision") == expected
        if trace.get("critical_regression"):
            metrics["escape_den"] += 1
            metrics["escape_num"] += bool(trace.get("regression_escaped"))
        reasons = []
        if trace.get("decision") != expected:
            reasons.append("learning_decision_mismatch")
        if trace.get("critical_regression") and trace.get("regression_escaped"):
            reasons.append("critical_regression_escaped")
        if reasons:
            failures.append(_failure(scenario, "learning", reasons))
    return {
        "learning_candidate_precision": _metric("learning_candidate_precision", metrics["candidate_num"], metrics["candidate_den"]),
        "learning_regression_escape_rate": _metric("learning_regression_escape_rate", metrics["escape_num"], metrics["escape_den"]),
    }, failures


def _evaluate(scenarios: list[dict[str, Any]]) -> tuple[dict[str, Metric], list[dict[str, Any]]]:
    memory, memory_failures = _memory_metrics(
        scenario for scenario in scenarios if scenario["category"] in MEMORY_CATEGORIES
    )
    skills, skill_failures = _skill_metrics(
        scenario for scenario in scenarios if scenario["category"] in SKILL_CATEGORIES
    )
    continuity, continuity_failures = _continuity_metrics(
        scenario for scenario in scenarios if scenario["category"] in CONTINUITY_CATEGORIES
    )
    learning, learning_failures = _learning_metrics(
        scenario for scenario in scenarios if scenario["category"] in LEARNING_CATEGORIES
    )
    return {**memory, **skills, **continuity, **learning}, [
        *memory_failures,
        *skill_failures,
        *continuity_failures,
        *learning_failures,
    ]


def _slices(scenarios: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    dimensions = {
        "difficulty": ("easy", "medium", "hard"),
        "scope": ("user", "workspace"),
        "session_shape": ("single-session", "multi-session"),
        "memory_case": ("correction", "conflict", "stale"),
        "skill_case": ("exact", "ambiguous"),
    }
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for dimension, values in dimensions.items():
        result[dimension] = {}
        for value in values:
            selected = [
                scenario
                for scenario in scenarios
                if (
                    scenario.get(dimension) == value
                    if dimension == "difficulty"
                    else value in scenario.get("slices", {}).get(dimension, [])
                    or scenario.get("slices", {}).get(dimension) == value
                )
            ]
            metrics, _ = _evaluate(selected)
            result[dimension][value] = {
                "scenario_count": len(selected),
                "metrics": {name: metric.as_dict() for name, metric in metrics.items()},
            }
    return result


def _feature_flags() -> dict[str, str]:
    names = (
        "VEYA_MEMORY_V2",
        "VEYA_MEMORY_CANDIDATES",
        "VEYA_MEMORY_CONFLICT_DETECTION",
        "VEYA_SKILL_V2",
        "VEYA_SKILL_TEACHING",
        "VEYA_SKILL_VERSIONING",
        "VEYA_CONTINUITY_V1",
        "VEYA_LONG_TERM_LEARNING",
        "VEYA_PERSONAL_AGENT_EVAL",
    )
    return {name: os.environ.get(name, "default:true") for name in names}


def run_benchmark(
    root: Path | None = None,
    *,
    output_dir: Path | None = None,
    git_sha: str | None = None,
    write_outputs: bool = True,
) -> dict[str, Any]:
    root = root or Path(__file__).resolve().parent
    manifest, scenarios = load_dataset(root)
    run_id = f"personal-gold-{uuid.uuid4().hex}"
    metrics, failures = _evaluate(scenarios)
    counts = Counter(scenario["category"] for scenario in scenarios)
    difficulties = Counter(scenario["difficulty"] for scenario in scenarios)
    report: dict[str, Any] = {
        "eval_run_id": run_id,
        "dataset_version": manifest["dataset_version"],
        "scenario_schema_version": manifest["schema_version"],
        "label_version": manifest["label_version"],
        "scenario_count": len(scenarios),
        "approved_count": len(scenarios),
        "category_distribution": dict(sorted(counts.items())),
        "difficulty_distribution": dict(sorted(difficulties.items())),
        "git_sha": git_sha or _git_sha(),
        "model": None,
        "model_params": {},
        "feature_flags": _feature_flags(),
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "timestamp": datetime.now(UTC).isoformat(),
        "judge": {
            "mode": "deterministic_replay_contract",
            "llm_judge": "not_used",
            "human_gold_required": True,
        },
        "metrics": {name: metric.as_dict() for name, metric in metrics.items()},
        "slices": _slices(scenarios),
        "failure_count": len(failures),
        "hardest_failures": sorted(
            failures, key=lambda failure: (-len(failure["reasons"]), failure["scenario_id"])
        )[:20],
        "status": "PASS" if all(metric.passed is not False for metric in metrics.values()) else "FAIL",
    }
    report["dataset_file_hashes"] = manifest.get("file_hashes", {})
    report["dataset_content_hash"] = _json_hash(
        [{"scenario_id": scenario["scenario_id"], "labels": scenario} for scenario in scenarios]
    )
    if write_outputs:
        output_dir = output_dir or root / "results"
        output_dir.mkdir(parents=True, exist_ok=True)
        failures_dir = root / "failures"
        failures_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / f"{manifest['dataset_version']}-{report['git_sha'][:12]}.json"
        md_path = output_dir / f"{manifest['dataset_version']}-{report['git_sha'][:12]}.md"
        latest_json = output_dir / "latest.json"
        latest_md = output_dir / "latest.md"
        encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        for path in (json_path, latest_json):
            path.write_text(encoded, encoding="utf-8")
        markdown = render_markdown(report)
        for path in (md_path, latest_md):
            path.write_text(markdown, encoding="utf-8")
        failure_path = failures_dir / f"{run_id}.jsonl"
        failure_path.write_text(
            "".join(json.dumps({**failure, "eval_run_id": run_id}, ensure_ascii=False) + "\n" for failure in failures),
            encoding="utf-8",
        )
        report["output"] = {
            "json": str(json_path.relative_to(root)),
            "markdown": str(md_path.relative_to(root)),
            "failures": str(failure_path.relative_to(root)),
        }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Personal Agent Gold Benchmark — {report['dataset_version']}",
        "",
        f"- Eval run: `{report['eval_run_id']}`",
        f"- Git SHA: `{report['git_sha']}`",
        f"- Scenarios: `{report['approved_count']}/{report['scenario_count']}` approved",
        f"- Labels: `{report['label_version']}` (human-reviewed fixtures only)",
        f"- Runtime schema: `{report['runtime_schema_version']}`",
        f"- Status: **{report['status']}**",
        "",
        "Metrics use explicit numerator/denominator definitions and Wilson 95% confidence intervals. The replay judge is deterministic; no LLM judge is used.",
        "",
        "| Metric | Current | Target | Pass | N | 95% CI |",
        "|---|---:|---:|:---:|---:|---|",
    ]
    for name, value in report["metrics"].items():
        rate = "null" if value["rate"] is None else f"{value['rate']:.4f}"
        target = f"{value['target_operator']}{value['target']:.2f}"
        passed = "—" if value["passed"] is None else ("PASS" if value["passed"] else "FAIL")
        ci = "null" if value["ci95"] is None else f"[{value['ci95']['low']:.4f}, {value['ci95']['high']:.4f}]"
        lines.append(
            f"| `{name}` | {rate} | {target} | {passed} | {value['numerator']}/{value['denominator']} | {ci} |"
        )
    lines.extend(
        [
            "",
            "## Dataset",
            "",
            f"Category distribution: `{json.dumps(report['category_distribution'], ensure_ascii=False, sort_keys=True)}`",
            "",
            f"Difficulty distribution: `{json.dumps(report['difficulty_distribution'], ensure_ascii=False, sort_keys=True)}`",
            "",
            f"Failure scenarios: `{report['failure_count']}`",
            "",
            "## Release interpretation",
            "",
            "These numbers measure the approved deterministic Gold replay contract at the recorded commit. They are not a claim about unlabeled production conversations. New production shadow candidates remain outside the benchmark until human review and a dataset version bump.",
            "",
        ]
    )
    return "\n".join(lines)


def record_shadow_candidate(
    root: Path,
    *,
    category: str,
    source_event_id: str,
    source_task_id: str | None = None,
    outcome: str = "",
    correction: bool = False,
) -> dict[str, Any]:
    """Store a privacy-safe candidate; this never changes the Gold dataset."""
    candidate = {
        "candidate_id": f"candidate-{uuid.uuid4().hex}",
        "category": category,
        "source_event_hash": _json_hash(source_event_id),
        "source_task_hash": _json_hash(source_task_id) if source_task_id else None,
        "outcome_hash": _json_hash(outcome) if outcome else None,
        "correction": correction,
        "review_status": "draft",
        "created_at": datetime.now(UTC).isoformat(),
    }
    path = root / "candidate_eval_cases.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(candidate, ensure_ascii=False, sort_keys=True) + "\n")
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--git-sha")
    args = parser.parse_args(argv)
    try:
        report = run_benchmark(args.root, output_dir=args.output_dir, git_sha=args.git_sha)
    except ValueError as exc:
        print(f"INSUFFICIENT_GOLD_DATA: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
