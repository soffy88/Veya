"""9. Execution-model (sync/async) contract collection & drift protection.

Collects the async status of every exported top-level function across
oprim/oskill/omodul, then compares against a committed baseline JSON.
A silent sync→async (or async→sync) flip is a MAJOR breaking change
(§0.2) and MUST fail CI.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

TARGET_LAYERS = ["oprim", "oskill", "omodul"]


def collect_async_contracts(root_dir: Path) -> dict[str, bool]:
    """Collect async status for every exported top-level function."""
    contracts: dict[str, bool] = {}
    for layer in TARGET_LAYERS:
        layer_dir = root_dir / layer
        if not layer_dir.exists():
            continue

        for py_file in layer_dir.glob("*.py"):
            if py_file.name.startswith("_"):
                continue

            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and not node.name.startswith("_"):
                    key = f"{layer}.{py_file.stem}.{node.name}"
                    contracts[key] = isinstance(node, ast.AsyncFunctionDef)
    return contracts


def write_baseline(root_dir: Path, baseline_file: Path) -> None:
    """Write the current contract snapshot as the new baseline."""
    contracts = collect_async_contracts(root_dir)
    baseline_file.parent.mkdir(parents=True, exist_ok=True)
    baseline_file.write_text(
        json.dumps(contracts, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def check_async_contract(root_dir: Path, baseline_file: Path | None = None) -> list[str]:
    errors: list[str] = []
    current_contracts = collect_async_contracts(root_dir)

    if baseline_file and baseline_file.exists():
        baseline_contracts = json.loads(baseline_file.read_text(encoding="utf-8"))
        for func_key, is_async in current_contracts.items():
            if func_key in baseline_contracts:
                old_async = baseline_contracts[func_key]
                if old_async != is_async:
                    errors.append(
                        f"[SPEC §0.2 Async Contract BREAKING] '{func_key}' execution model changed "
                        f"from {'async' if old_async else 'sync'} to "
                        f"{'async' if is_async else 'sync'}. This is a MAJOR Breaking Change!"
                    )
    return errors
