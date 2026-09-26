#!/usr/bin/env python3
"""Reject tests that report success without observing a behavior."""

from __future__ import annotations

import ast
import pathlib
import sys


def _assertion_violations(path: pathlib.Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [f"{path}: cannot parse ({exc})"]
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if isinstance(test, ast.Constant) and test.value is True:
            violations.append(f"{path}:{node.lineno}: unconditional assert True")
        if (
            isinstance(test, ast.BoolOp)
            and isinstance(test.op, ast.Or)
            and any(
                isinstance(value, ast.Constant) and value.value is True for value in test.values
            )
        ):
            violations.append(f"{path}:{node.lineno}: assertion made always true by `or True`")
    return violations


def _skip_violations(path: pathlib.Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == "skip":
            has_reason = any(keyword.arg == "reason" for keyword in node.keywords)
            has_reason = has_reason or bool(node.args)
            if not has_reason:
                violations.append(f"{path}:{node.lineno}: pytest.skip without reason")
    return violations


def main(root_arg: str = ".") -> int:
    root = pathlib.Path(root_arg).resolve()
    violations = []
    for path in sorted((root / "tests").rglob("*.py")):
        violations.extend(_assertion_violations(path))
        violations.extend(_skip_violations(path))
    if violations:
        print("[FAIL] test evidence integrity violation(s):")
        print("\n".join(f"  - {item}" for item in violations))
        return 1
    print("[OK] test evidence integrity enforced")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
