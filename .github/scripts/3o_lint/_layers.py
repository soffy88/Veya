"""Shared layer-directory resolution for the 3O lint suite.

Supports three repo layouts so the same 9 checks work against both the Veya
repo (``veya/obase``) and the canonical 3O libraries mounted as submodules
(``platform/3O/<layer>/<layer>`` per SPEC v3.0 §2.1 independent-package layout).

Resolution order for a given layer name:
  1. ``<root>/<layer>/<layer>``   — 3O main-library package layout
  2. ``<root>/<layer>``           — flat layout
  3. ``<root>/veya/<layer>``      — Veya project-library layout (veya/obase, veya/oservi ...)

The ``veya/<layer>`` layout is the Veya project layer: the ``veya`` package is
itself organized as 3O layers so the same 9 checks cover the project library,
not just the mounted 3O main libraries.
"""

from __future__ import annotations

from pathlib import Path

LAYERS = ("oprim", "oskill", "omodul", "obase", "oservi")


def resolve_layer_dirs(root: Path) -> dict[str, Path]:
    """Return {layer_name: Path} for every 3O layer present under ``root``."""
    layers: dict[str, Path] = {}
    for name in LAYERS:
        nested = root / name / name
        direct = root / name
        veya_nested = root / "veya" / name
        if nested.is_dir():
            layers[name] = nested
        elif direct.is_dir():
            layers[name] = direct
        elif veya_nested.is_dir():
            layers[name] = veya_nested
    return layers
