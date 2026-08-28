"""CLI adapter for the read-only Harness Doctor."""

from __future__ import annotations

import argparse
import json

from runtime.harness.doctor import run_harness_doctor


def run_harness_doctor_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="veya harness doctor", description="Harness 自检")
    parser.add_argument("--path", default=".", help="workspace path")
    parser.add_argument(
        "--unattended", action="store_true", help="检查 unattended coding readiness"
    )
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args(argv)
    report = run_harness_doctor(args.path, unattended=args.unattended)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.status)
        for check in report.checks:
            print(f"[{check.status.upper()}] {check.name}: {check.detail}")
        if report.degraded_reasons:
            print("Degraded:")
            for reason in report.degraded_reasons:
                print(f"- {reason}")
        if report.blockers:
            print("Blocked:")
            for blocker in report.blockers:
                print(f"- {blocker}")
    # Degraded is an actionable report, not a command failure.  Blocked is
    # non-zero so unattended automation cannot mistake an unsafe setup for OK.
    return 2 if report.status == "HARNESS_BLOCKED" else 0


__all__ = ["run_harness_doctor_cli"]
