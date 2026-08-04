"""8. oservi dependency-inversion check: no hardcoded concrete business imports
(implementations MUST be injected via ServiceManifest)."""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN_CONCRETE_IMPORTS = {"oprim", "oskill", "omodul"}


def check_oservi_injection(oservi_dir: Path) -> list[str]:
    errors: list[str] = []
    if not oservi_dir.exists():
        return errors

    for py_file in oservi_dir.glob("*.py"):
        if py_file.name.startswith("_"):
            continue

        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                root_mod = node.module.split(".")[0]
                if root_mod in FORBIDDEN_CONCRETE_IMPORTS:
                    errors.append(
                        f"[SPEC §7.2 Dependency Inversion] {py_file.name}:{node.lineno} -> "
                        f"oservi skeleton hardcodes import from '{node.module}'. "
                        "Implementations MUST be injected via ServiceManifest."
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".")[0]
                    if root_mod in FORBIDDEN_CONCRETE_IMPORTS:
                        errors.append(
                            f"[SPEC §7.2 Dependency Inversion] {py_file.name}:{node.lineno} -> "
                            f"oservi skeleton hardcodes import '{alias.name}'. "
                            "Implementations MUST be injected via ServiceManifest."
                        )
    return errors
