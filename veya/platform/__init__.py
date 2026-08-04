"""veya.platform — canonical 3O main-library assembly layer (SPEC v3.0 §2.1/§1.4).

Veya is assembled FROM the 3O main libraries (oprim / oskill / omodul / obase /
oservi), which are mounted as git submodules under ``platform/3O``. This module
is the single choke point for that assembly:

- injects the submodule paths into ``sys.path`` (self-contained; works after a
  plain ``git clone --recursive`` — no private index, no extra config);
- lazily resolves main-library packages so Veya's lightweight runtime never
  pays for heavy third-party deps (rapidfuzz / anthropic / asyncpg / mcp / ...)
  unless a consumer actually imports them;
- degrades gracefully: if the submodules are absent, ``available()`` is False
  and every accessor raises an informative error instead of a bare ImportError.

Single-source contract (§1.4): business/infra logic lives in the main library;
Veya only adapts/assembles. Any symbol that already exists in a main library
MUST NOT be re-implemented in Veya — re-export or adapt it here instead.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

_MAINLIBS = ("obase", "oprim", "oskill", "omodul", "oservi")

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parents[1]
_3O_ROOT = _PROJECT_ROOT / "platform" / "3O"

_injected: set[str] = set()


def _ensure_paths() -> None:
    """Idempotently add each main-library package dir to sys.path."""
    for lib in _MAINLIBS:
        pkg = _3O_ROOT / lib
        if pkg.is_dir() and str(pkg) not in sys.path and str(pkg) not in _injected:
            sys.path.insert(0, str(pkg))
            _injected.add(str(pkg))


def available(lib: str = "obase") -> bool:
    """True if the given main library is mounted (submodule present)."""
    return (_3O_ROOT / lib).is_dir()


def root(lib: str = "obase") -> Path:
    """Absolute path to a mounted main library, or raise a clear error."""
    p = _3O_ROOT / lib
    if not p.is_dir():
        raise RuntimeError(
            f"3O main library '{lib}' is not mounted. Clone with "
            "`git clone --recursive` (submodules live under platform/3O/)."
        )
    return p


def load(lib: str) -> Any:
    """Import a main-library package (lazy) and return the module object.

    e.g. ``obase = veya.platform.load("obase")`` then ``obase.Cache``.
    """
    if lib not in _MAINLIBS:
        raise ValueError(f"unknown main library {lib!r}; expected one of {_MAINLIBS}")
    root(lib)  # presence check with a clear error
    _ensure_paths()
    return importlib.import_module(lib)


# --- convenience accessors for the obase core used by Veya ------------------
def obase() -> object:
    """Lazy ``import obase`` (core submodules; optional deps lazy-resolved)."""
    return load("obase")


def oprim() -> object:
    """Lazy ``import oprim``."""
    return load("oprim")


def oskill() -> object:
    """Lazy ``import oskill``."""
    return load("oskill")


def omodul() -> object:
    """Lazy ``import omodul``."""
    return load("omodul")


def oservi() -> object:
    """Lazy ``import oservi``."""
    return load("oservi")


__all__ = [
    "available",
    "load",
    "obase",
    "omodul",
    "oprim",
    "oservi",
    "oskill",
    "root",
]
