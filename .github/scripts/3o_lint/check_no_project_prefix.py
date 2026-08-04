"""2. No project/vendor prefix check on element and file names."""

from __future__ import annotations

import ast
from pathlib import Path

# Extendable banned-prefix set (project/vendor bound names).
BANNED_PREFIXES = ("aegis_", "finance_", "veya_", "vendor_", "proj_")
TARGET_LAYERS = ["oprim", "oskill", "omodul", "oservi"]


def check_no_project_prefix(root_dir: Path) -> list[str]:
    errors: list[str] = []
    for layer in TARGET_LAYERS:
        layer_dir = root_dir / layer
        if not layer_dir.exists():
            continue

        for py_file in layer_dir.glob("*.py"):
            if py_file.name.startswith("_"):
                continue

            # 1. file name
            if py_file.name.startswith(BANNED_PREFIXES):
                errors.append(
                    f"[SPEC §2.4 Naming] File '{py_file.name}' in '{layer}' uses illegal project/vendor prefix."
                )

            # 2. exported top-level function/class names
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            except SyntaxError:
                continue

            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (
                    not node.name.startswith("_") and node.name.startswith(BANNED_PREFIXES)
                ):
                    errors.append(
                        f"[SPEC §2.4 Naming] {py_file.name}:{node.lineno} -> Entity '{node.name}' "
                        "uses illegal project/vendor prefix."
                    )
    return errors
