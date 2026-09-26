#!/usr/bin/env python3
"""Sensor: Enforces zero semantic or execution imports in veya/operations (spec §1, §70)."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def main() -> int:
    ops_dir = Path("veya/operations")
    if not ops_dir.exists():
        print("[FAIL] veya/operations not found")
        return 1

    forbidden_imports = [
        "server.coordinator_master",
        "veya.obase.llm",
        "runtime.goal_run",
        "runtime.coding",
        "veya.autonomous.decision",
    ]

    pattern = re.compile(r"^\s*(?:from|import)\s+([a-zA-Z0-9_\.]+)", re.MULTILINE)
    violations = []
    for py_file in ops_dir.glob("*.py"):
        content = py_file.read_text()
        for mod in pattern.findall(content):
            for forbidden in forbidden_imports:
                if mod == forbidden or mod.startswith(f"{forbidden}."):
                    violations.append((py_file.name, mod))

    if violations:
        print(f"[FAIL] Semantic imports found in veya/operations: {violations}")
        return 1

    print("[OK] check_operations_semantic_imports: zero semantic imports in veya/operations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
