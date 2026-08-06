"""Veya Static Invariant Engine — 前瞻性与静态不变量校验引擎(薄适配层)。

3O 单一来源 (§1.4): 本体已固化为主库 oprim._lookahead_scan.scan_lookahead
(纯 AST 硬扫描, 不依赖 LLM "猜"):
  L1 未来函数 shift(-k)      → VIOLATION (标签列降级 WARNING)
  L2 未来行索引 iloc[i+1]     → VIOLATION
  L3 np.roll 负偏移           → VIOLATION
  L4 滚动统计量未 shift(1)    → WARNING (数据泄漏)
  L5 volume 分母除零          → WARNING (集合竞价 VWAP 崩溃点)
本层保留脚手架 API: VeyaStaticInvariant.check(strategy_code, filename)。
"""

from __future__ import annotations

from typing import Any

from veya.platform import oprim as _load_oprim

_oprim = _load_oprim()


class VeyaStaticInvariant:
    """静态不变量校验引擎: 代码进协处理器之前的"数学级法律"检查。"""

    @staticmethod
    def check(strategy_code: str, filename: str = "<strategy>") -> dict[str, Any]:
        """对策略源码执行 AST 硬扫描.

        Returns:
            {"verdict": "pass"|"review"|"block"|"error", "findings": [...],
             "violations": [...], "warnings": [...], "summary": {...}}
        """
        return _oprim._lookahead_scan.scan_lookahead(strategy_code, filename=filename)
