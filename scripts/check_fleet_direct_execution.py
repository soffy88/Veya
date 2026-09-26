#!/usr/bin/env python3
"""Architecture sensor: Verify Fleet does not perform direct command execution (spec §55).

Invariants:
- FLEET_DIRECT_EXECUTION=0: Fleet is scheduling/placement only, never invokes executor directly or spawns subprocesses.
"""

from __future__ import annotations

import ast
import pathlib
import sys

FORBIDDEN_CALLS = {
    "run",
    "Popen",
    "call",
    "check_call",
    "check_output",
    "system",
    "popen",
    "spawn",
}

FORBIDDEN_MODULES = {
    "subprocess",
}


def scan_direct_execution(root: pathlib.Path) -> list[str]:
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
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in FORBIDDEN_MODULES:
                        violations.append(
                            f"{path.relative_to(root)}:{node.lineno}: imports forbidden execution module '{alias.name}'"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module in FORBIDDEN_MODULES:
                    violations.append(
                        f"{path.relative_to(root)}:{node.lineno}: imports from forbidden execution module '{node.module}'"
                    )
            elif isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in FORBIDDEN_CALLS
                    and isinstance(func.value, ast.Name)
                    and func.value.id in ("os", "subprocess")
                ):
                    violations.append(
                        f"{path.relative_to(root)}:{node.lineno}: direct execution call '{func.value.id}.{func.attr}'"
                    )

    return violations


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    violations = scan_direct_execution(root)
    if violations:
        print("[FAIL] check_fleet_direct_execution found violations:")
        for v in violations:
            print(f"  {v}")
        return 1
    print("[OK] check_fleet_direct_execution: FLEET_DIRECT_EXECUTION=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
