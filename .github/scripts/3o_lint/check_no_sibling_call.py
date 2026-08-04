"""3. Same-layer bare-call check:
- oprim: must not call siblings directly
- omodul: must not call siblings directly (orchestration goes through oservi)
- oskill: limited mutual calls allowed (depth <= 2, MUST be disclosed in docstring)
"""

from __future__ import annotations

import ast
from pathlib import Path

from _layers import resolve_layer_dirs


def _get_imports(tree: ast.AST) -> dict[str, str]:
    """Extract the import map, e.g. 'other_oprim' -> 'oprim'."""
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root_mod = node.module.split(".")[0]
            for alias in node.names:
                imports[alias.name] = root_mod
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.name.split(".")[0]] = alias.name.split(".")[0]
    return imports


def check_no_sibling_call(root_dir: Path) -> list[str]:
    errors: list[str] = []
    layers = resolve_layer_dirs(root_dir)

    # A. oprim & omodul: same-layer bare calls forbidden
    for layer in ["oprim", "omodul"]:
        layer_dir = layers.get(layer)
        if layer_dir is None:
            continue

        for py_file in layer_dir.glob("*.py"):
            if py_file.name.startswith("_"):
                continue

            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            imports = _get_imports(tree)

            for func_name, mod in imports.items():
                if mod == layer:
                    errors.append(
                        f"[SPEC §1.2 Sibling Call] {py_file.name} illegally imports sibling "
                        f"'{func_name}' from the same layer '{layer}'."
                    )

    # B. oskill mutual-call constraints (depth <= 2 & docstring disclosure)
    oskill_dir = layers.get("oskill")
    if oskill_dir is not None:
        oskill_deps: dict[str, list[str]] = {}

        for py_file in oskill_dir.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            imports = _get_imports(tree)

            sibling_calls = [name for name, mod in imports.items() if mod == "oskill"]
            oskill_deps[py_file.stem] = sibling_calls

            # calling sibling oskills requires docstring disclosure
            if sibling_calls:
                doc = ast.get_docstring(tree) or ""
                if "oskill" not in doc.lower():
                    errors.append(
                        f"[SPEC §1.2 oskill Constraint] {py_file.name} calls sibling oskills "
                        f"{sibling_calls} but lacks docstring disclosure."
                    )

        # call-chain depth check (A -> B -> C is depth 3, illegal)
        for caller, callee_list in oskill_deps.items():
            for callee in callee_list:
                nested_callees = oskill_deps.get(callee, [])
                if nested_callees:
                    errors.append(
                        f"[SPEC §1.2 Call Depth] Call chain '{caller} -> {callee} -> {nested_callees}' "
                        "exceeds max allowed depth of 2."
                    )

    return errors
