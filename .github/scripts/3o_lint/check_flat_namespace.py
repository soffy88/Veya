"""1. Flat-namespace check: oprim/oskill/omodul MUST stay flat (no domain subdirs)."""

from __future__ import annotations

from pathlib import Path

from _layers import resolve_layer_dirs

MODULE_LAYERS = ["oprim", "oskill", "omodul"]


def check_flat_namespace(root_dir: Path) -> list[str]:
    errors: list[str] = []
    layers = resolve_layer_dirs(root_dir)
    for layer in MODULE_LAYERS:
        layer_dir = layers.get(layer)
        if layer_dir is None:
            continue

        for item in layer_dir.iterdir():
            # ignore private dirs and pycache
            if item.is_dir() and not item.name.startswith(("_", ".")):
                errors.append(
                    f"[SPEC §2.2 Flat Namespace] Layer '{layer}' contains illegal subdirectory '{item.name}'. "
                    "3O layers MUST be flat without domain submodules."
                )
    return errors
