"""Veya Adversarial Chamber — 红蓝对抗审判庭(薄适配层)。

3O 单一来源 (§1.4): 本体已固化为主库 omodul.adversarial_chamber.adversarial_chamber
(蓝队辩护 → 红队质疑 → 主脑裁决, 前置 oprim._lookahead_scan 静态证据)。
本层保留脚手架 API: VeyaAdversarialChamber.review(strategy_code, ...)。

双模式:
  - 确定性模式 (默认, llm_fn=None): 静态证据 + 规则化辩论, 离线安全
  - LLM 模式 (注入 llm_fn): 真实红蓝辩论 (llm_fn 复用 veya.llm.llm_call 契约)
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from veya.platform import omodul as _load_omodul

_omodul = _load_omodul()


def _make_llm_caller(llm_fn: Callable | None) -> Callable | None:
    """把外部 llm_fn 包装成 omodul caller 契约: async (messages=..., tools=..., max_tokens=...)。"""
    if llm_fn is None:
        return None

    async def caller(
        messages: list, tools: list | None = None, max_tokens: int = 4096, **kwargs: Any
    ) -> dict:
        return await llm_fn(messages, tools=tools, max_tokens=max_tokens, **kwargs)

    return caller


class VeyaAdversarialChamber:
    """红蓝对抗审判庭: 策略代码交付前必须过的一关。"""

    def __init__(
        self,
        llm_fn: Callable | None = None,
        output_dir: str | Path = "~/.veya/reports/adversarial",
        safety_threshold: float = 70.0,
    ):
        self._llm_fn = _make_llm_caller(llm_fn)
        self.output_dir = Path(output_dir).expanduser()
        self.safety_threshold = safety_threshold

    async def review(
        self,
        strategy_code: str,
        strategy_name: str = "unnamed_strategy",
        context: str = "",
    ) -> dict[str, Any]:
        """对策略代码执行红蓝对抗审判, 产出《红蓝对抗审计报告》。

        Returns:
            {status: blocked|needs_review|approved, safety_score_before,
             safety_score_after, red_points, judge_fixes, final_code,
             report_path, fingerprint, ...}
        """
        config = _omodul.AdversarialChamberConfig(
            llm_fn=self._llm_fn,
            safety_threshold=self.safety_threshold,
        )
        input_data = _omodul.AdversarialChamberInput(
            strategy_code=strategy_code,
            strategy_name=strategy_name,
            context=context,
        )
        return await _omodul.adversarial_chamber(config, input_data, self.output_dir)
