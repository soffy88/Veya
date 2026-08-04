#!/usr/bin/env python3
"""G16 — enforce English docstrings on the modern core (CI gate).

Checks that every docstring in the modern-core file set is ASCII-only
(no CJK characters). Legacy modules (compat.py, streaming.py, tools/, etc.)
are intentionally excluded and migrated incrementally.

Usage: python3 scripts/check_docstring_language.py
Exit code 0 = clean, 1 = violations found.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")

# Modern core owned by this repo's current engineering bar.
# NOTE: keep in sync with the CI lint job.
MODERN_CORE = [
    "veya/obase/__init__.py",
    "veya/obase/telemetry.py",
    "veya/obase/authz.py",
    "veya/llm.py",
    "veya/intent.py",
    "veya/sandbox.py",
    "veya/multimodal.py",
    "veya/errors.py",
    "veya/utils.py",
]

ROOT = pathlib.Path(__file__).resolve().parent.parent


def violations(path: pathlib.Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [f"  parse error: {exc}"]
    found: list[str] = []
    nodes = [tree] + [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    for node in nodes:
        doc = ast.get_docstring(node) or ""
        if CJK.search(doc):
            name = getattr(node, "name", "<module>")
            lineno = getattr(node, "lineno", 0)
            found.append(f"  {path} L{lineno} {name}: CJK docstring")
    return found


def main() -> int:
    problems: list[str] = []
    for rel in MODERN_CORE:
        p = ROOT / rel
        if not p.exists():
            problems.append(f"  missing: {rel}")
            continue
        problems.extend(violations(p))
    if problems:
        print("[G16] English-docstring gate FAILED:")
        for line in problems:
            print(line)
        return 1
    print(f"[G16] English-docstring gate OK ({len(MODERN_CORE)} files, ASCII docstrings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
