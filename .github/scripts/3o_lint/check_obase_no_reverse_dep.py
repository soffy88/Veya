"""7. obase zero-reverse-dependency check (never import oprim/oskill/omodul)."""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN_3O_LAYERS = {"oprim", "oskill", "omodul"}


def check_obase_no_reverse_dep(obase_dir: Path) -> list[str]:
    errors: list[str] = []
    if not obase_dir.exists():
        return errors

    for py_file in obase_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                root_mod = node.module.split(".")[0]
                if root_mod in FORBIDDEN_3O_LAYERS:
                    errors.append(
                        f"[SPEC §3.4 obase Dependency] {py_file.name}:{node.lineno} -> "
                        f"obase illegally imports from 3O layer '{node.module}'."
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".")[0]
                    if root_mod in FORBIDDEN_3O_LAYERS:
                        errors.append(
                            f"[SPEC §3.4 obase Dependency] {py_file.name}:{node.lineno} -> "
                            f"obase illegally imports '{alias.name}'."
                        )
    return errors
