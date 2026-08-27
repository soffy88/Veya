#!/usr/bin/env python3
"""Canonical direct-I/O gate used by CI, release, and security workflows.

This command intentionally reuses the existing scanner and legacy baseline.
The baseline comparison uses ``check_no_direct_io.stable_finding_key`` so source
line churn does not turn known findings into false "new" findings.  It does not
permit a new path/type/expression combination: those remain release blockers.
"""

from __future__ import annotations

import argparse
import json
import pathlib
from collections import Counter
from typing import Any

import check_no_direct_io as scanner


def scan(root: pathlib.Path, targets: list[str]) -> list[str]:
    findings: list[str] = []
    for path in scanner._iter_py_files(root, targets):
        if scanner._is_allowed(path, root) or scanner._has_allow_marker(path):
            continue
        findings.extend(scanner.check_file(path, root))
    return sorted(set(findings))


def _path(finding: str) -> str:
    match = scanner._FINDING_RE.match(finding)
    return match.group("path") if match else finding.split(":", 1)[0]


def _kind(finding: str) -> str:
    match = scanner._FINDING_RE.match(finding)
    if match is None:
        return "unknown"
    body = match.group("body")
    for kind in ("EXEC/NET", "FILE_W", "FILE_R", "EXEC", "NET"):
        if body.startswith(kind + " "):
            return kind
    return "unknown"


def _baseline_lines(path: pathlib.Path | None) -> list[str]:
    if path is None or not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_report(
    *,
    root: pathlib.Path,
    targets: list[str],
    baseline_path: pathlib.Path | None,
    findings: list[str],
    strict: bool,
) -> dict[str, Any]:
    baseline_lines = _baseline_lines(baseline_path)
    known, new = (
        ([], findings) if strict else scanner.classify_against_baseline(findings, baseline_lines)
    )
    exact_keys = set(baseline_lines)
    drift = [finding for finding in known if finding not in exact_keys]
    by_kind = Counter(_kind(finding) for finding in drift)
    by_root = Counter(_path(finding).split("/", 1)[0] for finding in drift)
    return {
        "schema_version": 1,
        "checker": "scripts/check_direct_io.py",
        "comparison": "stable_path_kind_expression",
        # Do not persist a developer/runner filesystem path in CI artifacts.
        "root": ".",
        "targets": targets,
        "baseline_path": str(baseline_path) if baseline_path else None,
        "baseline_entries": len(baseline_lines),
        "total_findings": len(findings),
        "exact_baseline_matches": len(set(findings) & exact_keys),
        "known_baseline": len(known),
        "known_baseline_line_drift": len(drift),
        "new_findings": len(new),
        "ignored_generated": 0,
        "ignored_fixtures": 0,
        "must_fix": len(new),
        "classification": {
            "production_runtime_code": len(drift),
            "tests": 0,
            "scripts": 0,
            "docs_examples": 0,
            "generated_vendor_build": 0,
            "migration_schema": 0,
            "safe_fixture_access": 0,
            "real_unsafe_new": len(new),
            "known_baseline": len(known),
            "checker_false_positive": 0,
        },
        "drift_by_kind": dict(sorted(by_kind.items())),
        "drift_by_top_level_directory": dict(sorted(by_root.items())),
        "new_findings_detail": new,
        "known_line_drift_detail": drift,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Canonical direct-I/O release gate")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--targets", nargs="+", default=None)
    parser.add_argument("--baseline", type=pathlib.Path, default=None)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--report", type=pathlib.Path, default=None)
    parser.add_argument("--fail-on-new", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    targets = args.targets or list(scanner.BUSINESS_DIRS)
    findings = scan(root, targets)
    report = build_report(
        root=root,
        targets=targets,
        baseline_path=args.baseline,
        findings=findings,
        strict=args.strict,
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    elif not args.quiet:
        print(
            "[DIRECT-IO] "
            f"total_findings={report['total_findings']} "
            f"known_baseline={report['known_baseline']} "
            f"line_drift={report['known_baseline_line_drift']} "
            f"new_findings={report['new_findings']} "
            f"baseline_entries={report['baseline_entries']}"
        )
    return 1 if args.fail_on_new and report["new_findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
