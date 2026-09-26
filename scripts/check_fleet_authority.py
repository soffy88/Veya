#!/usr/bin/env python3
"""Architecture sensor: Verify Fleet authority boundaries (spec §1, §55).

Invariants:
- FLEET_SEMANTIC_DECISION=0: Fleet does not make semantic decisions or completion judgements.
- FLEET_MISSION_MUTATION=0: Fleet does not mutate mission objectives or semantic status.
- FLEET_GOAL_MUTATION=0: Fleet does not create or replan GoalRuns.
"""

from __future__ import annotations

import ast
import pathlib
import sys

FORBIDDEN_CALLS = {
    "replan",
    "retask",
    "create_goal_run",
    "complete_mission",
    "abort_mission",
    "check_completion",
}

FORBIDDEN_ATTRS = {
    "current_hypothesis",
    "completion_gate",
    "assessor",
    "evaluator",
}


def scan_fleet_authority(root: pathlib.Path) -> list[str]:
    fleet_dir = root / "veya" / "fleet"
    if not fleet_dir.is_dir():
        return ["veya/fleet directory missing"]

    violations: list[str] = []
    for path in sorted(fleet_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except Exception as e:
            violations.append(f"{path}: parse error {e}")
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                func_name = ""
                if isinstance(func, ast.Name):
                    func_name = func.id
                elif isinstance(func, ast.Attribute):
                    func_name = func.attr
                if func_name in FORBIDDEN_CALLS:
                    violations.append(
                        f"{path.relative_to(root)}:{node.lineno}: forbidden semantic call '{func_name}'"
                    )
            elif isinstance(node, ast.Attribute):
                if node.attr in FORBIDDEN_ATTRS:
                    violations.append(
                        f"{path.relative_to(root)}:{node.lineno}: references semantic attribute '{node.attr}'"
                    )

    return violations


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    violations = scan_fleet_authority(root)
    if violations:
        print("[FAIL] check_fleet_authority found violations:")
        for v in violations:
            print(f"  {v}")
        return 1
    print(
        "[OK] check_fleet_authority: FLEET_SEMANTIC_DECISION=0, FLEET_MISSION_MUTATION=0, FLEET_GOAL_MUTATION=0"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
