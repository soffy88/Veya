"""Produce the first Veya Personal Intelligence Audit from Gold evidence."""

from __future__ import annotations

import argparse
import json
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _load_report(root: Path) -> dict[str, Any]:
    path = root / "results" / "latest.json"
    if not path.is_file():
        raise ValueError(f"Gold report is missing: {path}")
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("approved_count") != report.get("scenario_count"):
        raise ValueError("audit requires an all-approved Gold report")
    return report


def _load_failures(root: Path, eval_run_id: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for path in sorted((root / "failures").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if item.get("eval_run_id") == eval_run_id:
                    failures.append(item)
    return failures


def _failure_slices(report: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    output: dict[str, dict[str, dict[str, Any]]] = {}
    for dimension, values in report.get("slices", {}).items():
        output[dimension] = {}
        for value, body in values.items():
            failed = {
                name: metric
                for name, metric in body.get("metrics", {}).items()
                if metric.get("passed") is False
            }
            if failed:
                output[dimension][value] = {
                    "scenario_count": body.get("scenario_count", 0),
                    "failed_metrics": failed,
                }
    return output


def build_audit(root: Path) -> dict[str, Any]:
    report = _load_report(root)
    failures = _load_failures(root, report["eval_run_id"])
    domains = Counter(item.get("domain", "unknown") for item in failures)
    categories = Counter(item.get("scenario", {}).get("category", "unknown") for item in failures)
    reasons = Counter(reason for item in failures for reason in item.get("reasons", []))
    failed_metrics = [name for name, metric in report["metrics"].items() if not metric["passed"]]
    passing_metrics = [name for name, metric in report["metrics"].items() if metric["passed"]]
    critical = [
        item["scenario_id"]
        for item in failures
        if "critical_regression_escaped" in item.get("reasons", [])
    ]
    audit_id = f"pia-{report['eval_run_id']}-{uuid.uuid5(uuid.NAMESPACE_URL, report['eval_run_id']).hex[:12]}"
    return {
        "audit_id": audit_id,
        "audit_name": "Veya Personal Intelligence Audit",
        "audit_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "eval_run_id": report["eval_run_id"],
            "dataset_version": report["dataset_version"],
            "label_version": report["label_version"],
            "git_sha": report["git_sha"],
            "runtime_schema_version": report["runtime_schema_version"],
            "judge": report["judge"],
        },
        "sample": {
            "scenario_count": report["scenario_count"],
            "approved_count": report["approved_count"],
            "category_distribution": report["category_distribution"],
            "difficulty_distribution": report["difficulty_distribution"],
        },
        "decision": {
            "status": "BLOCKED_BY_GOLD_GATE" if failed_metrics else "PASS",
            "failed_metric_count": len(failed_metrics),
            "passed_metric_count": len(passing_metrics),
            "failed_metrics": failed_metrics,
            "passing_metrics": passing_metrics,
            "critical_regression_escape_count": len(critical),
            "critical_regression_escape_scenarios": critical,
        },
        "metrics": report["metrics"],
        "all_failure_slices": report.get("slices", {}),
        "failure_slices": _failure_slices(report),
        "failure_summary": {
            "failure_count": len(failures),
            "by_domain": dict(sorted(domains.items())),
            "by_category": dict(sorted(categories.items())),
            "by_reason": dict(sorted(reasons.items())),
        },
        "failures": failures,
        "interpretation": {
            "quality_claim": "approved deterministic Gold replay contract only",
            "production_telemetry": "not used as Gold",
            "llm_judge": "advisory only and not used",
            "service_health_is_separate": True,
        },
    }


def render_audit(audit: dict[str, Any]) -> str:
    source = audit["source"]
    decision = audit["decision"]
    lines = [
        "# Veya Personal Intelligence Audit",
        "",
        f"- Audit ID: `{audit['audit_id']}`",
        f"- Dataset: `{source['dataset_version']}`",
        f"- Eval run: `{source['eval_run_id']}`",
        f"- Gold SHA: `{source['git_sha']}`",
        f"- Approved scenarios: `{audit['sample']['approved_count']}/{audit['sample']['scenario_count']}`",
        f"- Decision: **{decision['status']}**",
        "",
        "This audit is based only on approved manually labelled deterministic replay evidence. It is not a production-conversation accuracy claim.",
        "",
        "## Complete metric ledger",
        "",
        "| Metric | Numerator / denominator | Rate | 95% CI | Target | Result |",
        "|---|---:|---:|---|---:|:---:|",
    ]
    for name, metric in audit["metrics"].items():
        ci = metric["ci95"]
        ci_text = "null" if ci is None else f"[{ci['low']:.4f}, {ci['high']:.4f}]"
        rate = "null" if metric["rate"] is None else f"{metric['rate']:.4f}"
        target = f"{metric['target_operator']}{metric['target']:.2f}"
        lines.append(
            f"| `{name}` | {metric['numerator']}/{metric['denominator']} | {rate} | {ci_text} | {target} | {'PASS' if metric['passed'] else 'FAIL'} |"
        )
    lines.extend(["", "## Failure slices", ""])
    for dimension, values in audit["failure_slices"].items():
        lines.append(f"### {dimension}")
        lines.append("")
        for value, body in values.items():
            lines.append(f"- `{value}` ({body['scenario_count']} scenarios):")
            for name, metric in body["failed_metrics"].items():
                ci = metric["ci95"]
                ci_text = "null" if ci is None else f"[{ci['low']:.4f}, {ci['high']:.4f}]"
                lines.append(
                    f"  - `{name}` {metric['numerator']}/{metric['denominator']} = {metric['rate']:.4f}; CI {ci_text}"
                )
        lines.append("")
    conclusion = (
        "The approved Gold contract meets every configured quality gate. The result is a deterministic replay audit, not a claim about unlabeled production conversations."
        if decision["status"] == "PASS"
        else "The runtime is production-healthy, but this baseline is not intelligence-quality-gate healthy. The failure corpus contains expected/actual/replay evidence for each failure."
    )
    lines.extend(
        [
            "## Failure summary",
            "",
            f"- Failing scenarios: `{audit['failure_summary']['failure_count']}`",
            f"- By domain: `{json.dumps(audit['failure_summary']['by_domain'], ensure_ascii=False, sort_keys=True)}`",
            f"- By category: `{json.dumps(audit['failure_summary']['by_category'], ensure_ascii=False, sort_keys=True)}`",
            f"- By reason: `{json.dumps(audit['failure_summary']['by_reason'], ensure_ascii=False, sort_keys=True)}`",
            f"- Critical regression escapes: `{decision['critical_regression_escape_count']}`",
            "",
            "## Failed scenarios",
            "",
            "| Scenario | Domain | Category | Reasons |",
            "|---|---|---|---|",
        ]
    )
    for failure in audit["failures"]:
        scenario = failure.get("scenario", {})
        lines.append(
            f"| `{failure['scenario_id']}` | {failure['domain']} | {scenario.get('category')} | {', '.join(failure.get('reasons', []))} |"
        )
    lines.extend(
        [
            "",
            "## Audit conclusion",
            "",
            conclusion,
            "",
        ]
    )
    return "\n".join(lines)


def write_audit(root: Path) -> dict[str, Any]:
    audit = build_audit(root)
    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    audit["output"] = {
        "json": "results/personal-intelligence-audit-latest.json",
        "markdown": "results/personal-intelligence-audit-latest.md",
    }
    encoded = json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    markdown = render_audit(audit)
    for path in (results / "personal-intelligence-audit-latest.json",):
        path.write_text(encoded, encoding="utf-8")
    for path in (results / "personal-intelligence-audit-latest.md",):
        path.write_text(markdown, encoding="utf-8")
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    audit = write_audit(args.root)
    print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if audit["decision"]["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
