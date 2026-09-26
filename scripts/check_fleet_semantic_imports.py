#!/usr/bin/env python3
"""Architecture sensor: Verify Fleet does not import semantic planning or LLM modules (spec §1, §44, §55).

Invariants:
- FLEET_SEMANTIC_DECISION=0: Fleet does not import MasterAgent internals, LLM clients, or autonomous planners.
"""

from __future__ import annotations

import ast
import pathlib
import sys

FORBIDDEN_IMPORTS = {
    "server.coordinator_master",
    "veya.obase.llm",
    "veya.autonomous.assessor",
    "veya.autonomous.evaluator",
    "veya.autonomous.planner_adapter",
    "veya.autonomous.cycle",
}


def scan_semantic_imports(root: pathlib.Path) -> list[str]:
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
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]

            for name in imported:
                if name in FORBIDDEN_IMPORTS or any(
                    name.startswith(f"{fi}.") for fi in FORBIDDEN_IMPORTS
                ):
                    violations.append(
                        f"{path.relative_to(root)}:{node.lineno}: imports forbidden semantic module '{name}'"
                    )

    return violations


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    violations = scan_semantic_imports(root)
    if violations:
        print("[FAIL] check_fleet_semantic_imports found violations:")
        for v in violations:
            print(f"  {v}")
        return 1
    print("[OK] check_fleet_semantic_imports: no semantic planner or LLM imports in veya/fleet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
