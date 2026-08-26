"""Classify pytest summary failures against the explicit known baseline."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SUMMARY_LINE = re.compile(r"^(?:FAILED|ERROR) (?P<nodeid>[^ ]+?)(?: - .*)?$")


def parse_summary(text: str) -> set[str]:
    nodeids: set[str] = set()
    for line in text.splitlines():
        match = SUMMARY_LINE.match(line.strip())
        if match:
            nodeids.add(match.group("nodeid"))
    return nodeids


def compare(baseline_path: Path, output_path: Path) -> dict[str, object]:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    expected = {item["nodeid"] for item in baseline["tests"]}
    actual = parse_summary(output_path.read_text(encoding="utf-8"))
    return {
        "known": sorted(actual & expected),
        "new": sorted(actual - expected),
        "resolved": sorted(expected - actual),
        "actual_count": len(actual),
        "known_count": len(actual & expected),
        "new_count": len(actual - expected),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path(__file__).parents[1] / "tests" / "baseline_failures.json",
    )
    args = parser.parse_args()
    result = compare(args.baseline, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if result["new_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
