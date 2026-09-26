#!/usr/bin/env python3
"""Architecture sensor: Verify FleetRegistry does not overlap with other registries (spec §6, §55).

Invariants:
- REGISTRY_OVERLAP=0: FleetRegistry does not duplicate Mission, GoalRun, Execution, or Channel registries.
"""

from __future__ import annotations

import ast
import pathlib
import sys

OVERLAPPING_TERMS = {
    "mission_registry",
    "goal_registry",
    "goalrun_registry",
    "execution_registry",
    "channel_registry",
    "session_registry",
}

OVERLAPPING_METHODS = {
    "create_goal",
    "create_mission",
    "register_channel",
    "append_observation",
    "record_execution",
}


def scan_registry_overlap(root: pathlib.Path) -> list[str]:
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
            if isinstance(node, ast.FunctionDef):
                if node.name.lower() in OVERLAPPING_METHODS:
                    violations.append(
                        f"{path.relative_to(root)}:{node.lineno}: defines overlapping registry method '{node.name}'"
                    )
            elif isinstance(node, ast.Name):
                if node.id.lower() in OVERLAPPING_TERMS:
                    violations.append(
                        f"{path.relative_to(root)}:{node.lineno}: references overlapping registry term '{node.id}'"
                    )
            elif isinstance(node, ast.Attribute) and node.attr.lower() in OVERLAPPING_TERMS:
                violations.append(
                    f"{path.relative_to(root)}:{node.lineno}: references overlapping attribute '{node.attr}'"
                )

    return violations


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    violations = scan_registry_overlap(root)
    if violations:
        print("[FAIL] check_fleet_registry_overlap found violations:")
        for v in violations:
            print(f"  {v}")
        return 1
    print("[OK] check_fleet_registry_overlap: REGISTRY_OVERLAP=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
