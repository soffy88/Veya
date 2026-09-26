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


def _candidate_3o_roots() -> list[Path]:
    """主库挂载根的候选位置 (正常仓库 / PyInstaller 打包产物)。"""
    roots: list[Path] = []
    # 1. 源码仓库: <repo>/platform/3O
    roots.append(_3O_ROOT)
    # 2. PyInstaller onedir: __file__ 在归档内不可用 → sys._MEIPASS 下的 datas 布局
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / "veya" / "platform" / "3O")
        roots.append(Path(meipass) / "platform" / "3O")
    # 去重保序
    seen: set[str] = set()
    out: list[Path] = []
    for p in roots:
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _package_is_mounted(base: Path, lib: str) -> bool:
    """True only when the gitlink contains an importable package checkout."""
    return (base / lib / lib / "__init__.py").is_file()


def _resolve_3o_root() -> Path:
    for root in _candidate_3o_roots():
        if _package_is_mounted(root, "obase"):
            return root
    return _3O_ROOT


def _ensure_paths() -> None:
    """Idempotently add each main-library package dir to sys.path."""
    base = _resolve_3o_root()
    for lib in _MAINLIBS:
        pkg = base / lib
        if (
            _package_is_mounted(base, lib)
            and str(pkg) not in sys.path
            and str(pkg) not in _injected
        ):
            sys.path.insert(0, str(pkg))
            _injected.add(str(pkg))


def available(lib: str = "obase") -> bool:
    """True if the given main library is mounted (submodule present)."""
    return _package_is_mounted(_resolve_3o_root(), lib)


def root(lib: str = "obase") -> Path:
    """Absolute path to a mounted main library, or raise a clear error."""
    base = _resolve_3o_root()
    p = base / lib
    if not _package_is_mounted(base, lib):
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
    module = importlib.import_module(lib)
    # The pinned oprim source renamed this public test/consumer constant from
    # ``observe_action`` to ``OBSERVE_ACTION``.  Keep the assembly boundary
    # compatible without modifying the external 3O checkout; this is a
    # namespace adapter, not a second implementation.
    if lib == "oprim":
        try:
            rollout = importlib.import_module("oprim._counterfactual_rollout")
            if not hasattr(rollout, "observe_action") and hasattr(rollout, "OBSERVE_ACTION"):
                rollout.observe_action = rollout.OBSERVE_ACTION
        except ImportError:
            pass
    return module


# --- convenience accessors for the obase core used by Veya ------------------
def obase() -> Any:
    """Lazy ``import obase`` (core submodules; optional deps lazy-resolved)."""
    return load("obase")


def oprim() -> Any:
    """Lazy ``import oprim``."""
    return load("oprim")


def oskill() -> Any:
    """Lazy ``import oskill``."""
    return load("oskill")


def omodul() -> Any:
    """Lazy ``import omodul``."""
    return load("omodul")


def oservi() -> Any:
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
