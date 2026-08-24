#!/usr/bin/env python3
"""docs/VEYA_10_OF_10_PLAN.md §13.3 / §28「建立最小安装 smoke」。

现有 CI `smoke` job 已经用 `pip install .`（不装任何 extras）跑通了功能级
产品 smoke（`veya --version` / `veya doctor` / `veya-headless` 出结果），
但没有单独校验 §13.3 定的依赖卫生指标本身。本脚本只做这一件事：

1. `import veya` 之后，pandas/numpy/pyarrow/matplotlib/plotly/networkx/
   textual 这些明确该走 extras 的重包不应该被跟着 eager import 进来
   （即使当前环境里装了它们——检查的是"有没有被动态触发", 不是"装没装"）。
2. 核心依赖表（`pyproject.toml::[project].dependencies`）条目数 ≤ 12
   （§13.3 目标指标）。

不测 `import veya p95 < 300ms` / `core wheel < 5MB` / cold-start < 2s ——
这些在共享 CI runner 上噪声太大，容易把偶发慢当成回归拉红，留给专门的
benchmark job（计划 §17）用更稳定的方法量。

用法：python scripts/check_minimal_install.py
"""

from __future__ import annotations

import pathlib
import re
import sys

_HEAVY_MODULES = (
    "pandas",
    "numpy",
    "pyarrow",
    "matplotlib",
    "plotly",
    "networkx",
    "textual",
)
_CORE_DEP_BUDGET = 12


def _check_core_dep_count(root: pathlib.Path) -> list[str]:
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"^dependencies = \[(.*?)^\]", pyproject, re.MULTILINE | re.DOTALL)
    if not match:
        return ["[FAIL] pyproject.toml 里找不到 [project].dependencies 数组"]
    count = len(re.findall(r'^\s*"', match.group(1), re.MULTILINE))
    if count > _CORE_DEP_BUDGET:
        return [f"[FAIL] 核心依赖 {count} 条, 超过 §13.3 budget ({_CORE_DEP_BUDGET})"]
    print(f"[OK] 核心依赖 {count} 条 (budget {_CORE_DEP_BUDGET})")
    return []


def _check_no_eager_heavy_import() -> list[str]:
    import veya  # noqa: F401

    loaded = [m for m in _HEAVY_MODULES if m in sys.modules]
    if loaded:
        return [
            "[FAIL] `import veya` 时被 eager import 触发了本该走 extras 的重包: "
            + ", ".join(loaded)
        ]
    print(f"[OK] `import veya` 未触发任何重包 eager import ({', '.join(_HEAVY_MODULES)})")
    return []


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    errors: list[str] = []
    errors += _check_core_dep_count(root)
    errors += _check_no_eager_heavy_import()

    if errors:
        for e in errors:
            print(e)
        return 1
    print("[OK] minimal install 依赖卫生校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
