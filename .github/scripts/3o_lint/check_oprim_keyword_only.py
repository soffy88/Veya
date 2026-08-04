"""4. oprim signature check: at most 1 positional arg, the rest keyword-only (*)."""

from __future__ import annotations

import ast
from pathlib import Path

# 'self'/'cls' belong to methods, not to the oprim positional contract.
_METHOD_SENTINELS = {"self", "cls"}


def check_oprim_keyword_only(oprim_dir: Path) -> list[str]:
    errors: list[str] = []
    if not oprim_dir.exists():
        return errors

    for py_file in oprim_dir.glob("*.py"):
        if py_file.name.startswith("_"):
            continue

        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))

        except SyntaxError:
            continue  # unparseable (newer py version) is not a violation
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue

                pos_args = [a for a in node.args.args if a.arg not in _METHOD_SENTINELS]
                if len(pos_args) > 1:
                    arg_names = [a.arg for a in pos_args]
                    errors.append(
                        f"[SPEC §4.4 oprim Signature] {py_file.name}:{node.lineno} -> Function "
                        f"'{node.name}' has {len(pos_args)} positional args {arg_names}. "
                        "Max allowed is 1. Add '*' to convert remaining args to keyword-only."
                    )
    return errors
