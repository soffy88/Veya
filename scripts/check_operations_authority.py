#!/usr/bin/env python3
"""Sensor: Enforces Veya Agent Operations V1 Authority Boundaries (spec §1, §70).

Verifies:
  OPERATIONS_SEMANTIC_DECISION=0
  OPERATIONS_DIRECT_EXECUTION=0
  OPERATIONS_DIRECT_PLACEMENT=0
  OPERATIONS_MISSION_MUTATION=0
  OPERATIONS_GOAL_MUTATION=0
  OPERATIONS_DIRECT_FLEET_STORE_WRITE=0
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def main() -> int:
    ops_dir = Path("veya/operations")
    if not ops_dir.exists():
        print("[FAIL] veya/operations not found")
        return 1

    forbidden_patterns = [
        (
            "OPERATIONS_SEMANTIC_DECISION",
            re.compile(r"\b(chat_stream|prompt|plan_mission|evaluate_evidence)\b"),
        ),
        (
            "OPERATIONS_DIRECT_EXECUTION",
            re.compile(r"\b(subprocess\.Popen|execute_command|run_command)\b"),
        ),
        (
            "OPERATIONS_DIRECT_PLACEMENT",
            re.compile(r"\b(assign_slot|schedule_placement|execute_placement)\b"),
        ),
        (
            "OPERATIONS_MISSION_MUTATION",
            re.compile(r"\b(complete_mission|fail_mission|cancel_mission)\b"),
        ),
        ("OPERATIONS_GOAL_MUTATION", re.compile(r"\b(mutate_goal|GoalRunState|transition_goal)\b")),
        (
            "OPERATIONS_DIRECT_FLEET_STORE_WRITE",
            re.compile(r"\b(fleet_registry\.sqlite|fleet_data/registry\.json)\b"),
        ),
    ]

    violations = []
    for py_file in ops_dir.glob("*.py"):
        content = py_file.read_text()
        for label, pat in forbidden_patterns:
            matches = pat.findall(content)
            if matches:
                violations.append((label, py_file.name, matches))

    if violations:
        print(f"[FAIL] Operations authority violations found: {violations}")
        return 1

    print(
        "[OK] check_operations_authority: all 6 operations authority invariants verified (0 drift)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
