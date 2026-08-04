"""1. Flat-namespace check: oprim/oskill/omodul MUST stay flat (no domain subdirs)."""

from __future__ import annotations

from pathlib import Path

MODULE_LAYERS = ["oprim", "oskill", "omodul"]


def check_flat_namespace(root_dir: Path) -> list[str]:
    errors: list[str] = []
    for layer in MODULE_LAYERS:
        layer_dir = root_dir / layer
        if not layer_dir.exists():
            continue

        for item in layer_dir.iterdir():
            # ignore private dirs and pycache
            if item.is_dir() and not item.name.startswith(("_", ".")):
                errors.append(
                    f"[SPEC §2.2 Flat Namespace] Layer '{layer}' contains illegal subdirectory '{item.name}'. "
                    "3O layers MUST be flat without domain submodules."
                )
    return errors
