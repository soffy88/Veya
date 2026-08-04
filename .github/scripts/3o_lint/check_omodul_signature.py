"""5. omodul standard signature & failure contract:
1. positional args must match the triplet (config, input_data, output_dir)
2. must NOT raise on failure (return status='failed' dict instead)
"""

from __future__ import annotations

import ast
from pathlib import Path

EXPECTED_ARGS = ["config", "input_data", "output_dir"]
_METHOD_SENTINELS = {"self", "cls"}


def check_omodul_signature(omodul_dir: Path) -> list[str]:
    errors: list[str] = []
    if not omodul_dir.exists():
        return errors

    for py_file in omodul_dir.glob("*.py"):
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

                # 1. signature check
                pos_args = [a.arg for a in node.args.args if a.arg not in _METHOD_SENTINELS]
                if pos_args[:3] != EXPECTED_ARGS:
                    errors.append(
                        f"[SPEC §5.2 omodul Signature] {py_file.name}:{node.lineno} -> Function "
                        f"'{node.name}' positional args are {pos_args[:3]}. "
                        f"Expected standard triplet: {EXPECTED_ARGS}."
                    )

                # 2. no-raise check (uncaught raise statements)
                for sub_node in ast.walk(node):
                    if isinstance(sub_node, ast.Raise) and sub_node.exc:
                        # re-raising asyncio.CancelledError is allowed
                        exc_name = ""
                        if isinstance(sub_node.exc, ast.Name):
                            exc_name = sub_node.exc.id
                        if exc_name != "CancelledError":
                            errors.append(
                                f"[SPEC §5.4 omodul No-Raise] {py_file.name}:{sub_node.lineno} -> "
                                f"omodul function '{node.name}' contains 'raise {exc_name}'. "
                                "omodul MUST NOT raise exceptions on failure "
                                "(return status='failed' dict instead)."
                            )
    return errors
