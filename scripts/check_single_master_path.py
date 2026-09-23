#!/usr/bin/env python3
"""Hard gate for the one production intelligence authority.

The archived coordinator may retain its implementation for checkpoint/test
compatibility, but no production module may import or invoke it.  This gate
deliberately scans source AST rather than relying on naming conventions.
"""

from __future__ import annotations

import ast
import pathlib
import sys

_SOURCE_DIRS = ("server", "runtime", "commands", "cli", "hooks", "registries", "tools")
_EXCLUDED = {"tests", "__pycache__", "venv", ".venv"}


def _files(root: pathlib.Path) -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    for name in _SOURCE_DIRS:
        directory = root / name
        if directory.is_dir():
            paths.extend(
                path
                for path in directory.rglob("*.py")
                if not any(part in _EXCLUDED for part in path.parts)
            )
    return sorted(paths)


def _violations(path: pathlib.Path, root: pathlib.Path) -> list[str]:
    # The archived implementation is intentionally retained for checkpoint
    # compatibility; the gate only constrains production callers.
    if path == root / "server" / "coordinator.py":
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [f"{path.relative_to(root)}: cannot parse ({exc})"]
    violations: list[str] = []
    for node in ast.walk(tree):
        imported: list[str] = []
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported = [node.module]
        if any(
            name == "server.coordinator" or name.startswith("server.coordinator.")
            for name in imported
        ):
            violations.append(f"{path.relative_to(root)}:{node.lineno}: imports server.coordinator")
        if isinstance(node, ast.Name) and node.id == "assemble_main_agent":
            violations.append(
                f"{path.relative_to(root)}:{node.lineno}: references assemble_main_agent"
            )
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "coordinator"
            and isinstance(node.value, ast.Name)
            and node.value.id == "server"
        ):
            violations.append(
                f"{path.relative_to(root)}:{node.lineno}: references server.coordinator"
            )
    return violations


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def main(root_arg: str | None = None) -> int:
    root = pathlib.Path(root_arg).resolve() if root_arg else _REPO_ROOT
    violations = [violation for path in _files(root) for violation in _violations(path, root)]
    if violations:
        print("[FAIL] single-master path violation(s):")
        print("\n".join(f"  - {item}" for item in violations))
        return 1
    print("[OK] single-master production path enforced")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
