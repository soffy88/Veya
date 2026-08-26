"""Hard release gate for the Personal Agent Gold benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.personal_agent_gold.benchmark import run_benchmark

HARD_GATES = {
    "memory_precision": (">=", 0.95),
    "memory_recall_when_needed": (">=", 0.90),
    "stale_memory_use_rate": ("<=", 0.01),
    "memory_correction_success_rate": (">=", 0.99),
    "wrong_skill_activation_rate": ("<=", 0.02),
    "continuity_task_recovery_accuracy": (">=", 0.98),
    "learning_regression_escape_rate": ("<=", 0.0),
}


def check_report(report: dict) -> list[str]:
    failures: list[str] = []
    if report.get("approved_count") != 170 or report.get("scenario_count") != 170:
        failures.append("approved Gold scenario count is not 170/170")
    for metric, (operator, target) in HARD_GATES.items():
        value = report.get("metrics", {}).get(metric, {})
        rate = value.get("rate")
        if rate is None:
            failures.append(f"{metric}: insufficient denominator")
        elif (operator == ">=" and rate < target) or (operator == "<=" and rate > target):
            failures.append(f"{metric}: {rate} violates {operator}{target}")
    if report.get("status") != "PASS":
        failures.append(f"benchmark status={report.get('status')}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    report = run_benchmark(args.root)
    failures = check_report(report)
    result = {
        "eval_run_id": report.get("eval_run_id"),
        "dataset_version": report.get("dataset_version"),
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
