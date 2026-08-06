"""veya_loop._assembly — 3O 主库装配器 (自包含, 不依赖 veya 项目)。

主库元素 (oprim/oskill/omodul/obase) 是独立主数据包。本模块按序解析:

  1. 直接 import (3O 主库已 pip 安装 —— 推荐的生产接线);
  2. 注入 git submodule 路径 (开发接线: 3O 库挂在本仓库 platform/3O 下)。

与 veya.platform 的区别: veya_loop 是独立包, 装配器完全自包含,
不 import veya 项目任何模块。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

_MAINLIBS = ("obase", "oprim", "oskill", "omodul", "oservi")

# src/veya_loop/_assembly.py → parents[2] = 本包所在仓库根
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[2]
_3O_ROOT = _REPO_ROOT / "platform" / "3O"

_injected = False


def _ensure_paths() -> None:
    """3O 主库未 pip 安装时, 把 submodule 路径注入 sys.path (幂等)。"""
    global _injected
    if _injected:
        return
    for lib in _MAINLIBS:
        pkg_dir = _3O_ROOT / lib
        if pkg_dir.is_dir() and str(pkg_dir) not in sys.path:
            sys.path.insert(0, str(pkg_dir))
    _injected = True


def load(lib: str) -> Any:
    """惰性导入主库包。"""
    if lib not in _MAINLIBS:
        raise ValueError(f"unknown main library {lib!r}; expected one of {_MAINLIBS}")
    _ensure_paths()
    try:
        return importlib.import_module(lib)
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"3O 主库 {lib!r} 不可用: 请 pip 安装该主库包, 或以 git submodule "
            f"形式挂载到 {_3O_ROOT} (见 README「接线」)"
        ) from exc


def obase() -> Any:
    return load("obase")


def oprim() -> Any:
    return load("oprim")


def oskill() -> Any:
    return load("oskill")


def omodul() -> Any:
    return load("omodul")


def oservi() -> Any:
    return load("oservi")


__all__ = ["load", "obase", "omodul", "oprim", "oservi", "oskill"]
