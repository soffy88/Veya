"""Veya Darwin Evolution — 达尔文算子自进化闭环(薄适配层)。

3O 单一来源 (§1.4): 本体已固化为主库 oservi.engines.darwin_evolution.DarwinEvolutionEngine
(影子测试 → 基因突变 → 并发回测优胜劣汰 → PRD 升级申请, 机制/业务分离)。
本层注入 Veya 侧业务填料:
  - backtest_fn → QuantCoprocessor 隔离沙箱回测
  - variant_fn  → Genesis LLM 语义级突变 (缺省 None → 引擎内确定性 AST 突变, 离线安全)
  - notify_fn   → NotificationCenter PRD 审批推送 (HITL_REQUIRED)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.notification_center import global_notifier
from server.quant_coprocessor import QuantCoprocessor
from veya.llm import llm_call
from veya.platform import oservi as _load_oservi

_oservi = _load_oservi()

logger = logging.getLogger("darwin")

_DEFAULT_STATE_DIR = "~/.veya/darwin"


class VeyaDarwinEvolution:
    """达尔文算子自进化: 衰减 → 突变 3 变种 → 并发回测 → 择优 → PRD 审批。"""

    def __init__(
        self,
        *,
        state_dir: str | Path = _DEFAULT_STATE_DIR,
        backtest_fn: Callable | None = None,
        variant_fn: Callable | None = None,
        notify_fn: Callable | None = None,
        asset_id: str = "BTCUSDT",
        start_date: str = "2022-01-01",
        end_date: str = "2024-12-31",
        shadow_min_samples: int = 5,
        decay_accuracy_below: float = 0.55,
        decay_slippage_above: float = 0.02,
        mutation_count: int = 3,
        genesis_llm: Callable | None = None,
    ):
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._coprocessor = QuantCoprocessor()
        self.asset_id = asset_id
        self.start_date = start_date
        self.end_date = end_date
        self._genesis_llm = genesis_llm

        engine_cfg = {
            "state_path": str(self.state_dir / "state.json"),
            "shadow_min_samples": shadow_min_samples,
            "decay_accuracy_below": decay_accuracy_below,
            "decay_slippage_above": decay_slippage_above,
            "mutation_count": mutation_count,
            "backtest_asset_id": asset_id,
        }
        self._engine = _oservi.DarwinEvolutionEngine(
            name="veya-darwin",
            backtest_fn=backtest_fn or self._default_backtest,
            variant_fn=variant_fn or self._default_variant_fn,
            notify_fn=notify_fn or self._default_notify,
            trigger={"on_interval": 3600},
            config=engine_cfg,
        )

    # ---- 业务填料: 回测 (Coprocessor 隔离沙箱) -----------------------------

    async def _default_backtest(self, code: str, params: dict, asset_id: str) -> dict[str, Any]:
        """把变体代码扔进 QuantCoprocessor 沙箱, 提取夏普/收益."""
        try:
            payload = json.loads(
                await self._coprocessor.execute_strategy(
                    strategy_code=code,
                    asset_id=asset_id,
                    start_date=self.start_date,
                    end_date=self.end_date,
                )
            )
        except FileNotFoundError as exc:
            return {"sharpe": None, "total_return": None, "error": f"行情数据缺失: {exc}"}
        except Exception as exc:  # pragma: no cover
            return {"sharpe": None, "total_return": None, "error": str(exc)}
        if payload.get("status") != "success":
            return {"sharpe": None, "total_return": None, "error": payload.get("traceback", "unknown")}
        metrics = payload.get("metrics", {})
        return {
            "sharpe": metrics.get("sharpe_ratio"),
            "total_return": metrics.get("total_return"),
            "error": None,
        }

    # ---- 业务填料: 基因突变 (Genesis LLM; 失败自动回落确定性突变) -----------

    async def _default_variant_fn(self, code: str, n: int) -> list[str]:
        """Genesis 语义级突变: LLM 生成 n 个变种 (调整平滑系数/加入非线性惩罚项等).

        无 Key/LLM 失败 → 返回空列表 → 引擎回落确定性 AST 突变.
        """
        if self._genesis_llm is None:
            return []
        prompt = (
            "你是 Genesis 进化引擎. 针对下面的量化算子生成 3 个基因突变变种.\n"
            "突变方向(至少覆盖): 1) 调整平滑系数/窗口参数 2) 加入非线性惩罚项抑制过拟合 "
            "3) 改变信号构造但保持语义.\n"
            "严格要求: 每个变种是完整可运行的 Python 代码(定义 run_strategy(df)), "
            "输出 JSON: {\"variants\": [\"<code1>\", \"<code2>\", \"<code3>\"]}\n"
            f"原始算子:\n```python\n{code}\n```"
        )
        try:
            resp = await llm_call([{"role": "user", "content": prompt}], max_tokens=8000)
            content = resp.get("content", "")
            if isinstance(content, list):
                content = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            variants = self._extract_variants(content)
            # 只保留可解析的变种 (不变量: 变种必须能通过静态编译)
            return [v for v in variants if self._compiles(v)][:n]
        except Exception as exc:  # pragma: no cover
            logger.warning("Genesis mutation failed (%s); falling back to deterministic mutation", exc)
            return []

    @staticmethod
    def _extract_variants(content: str) -> list[str]:
        import re

        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
        variants = data.get("variants", []) if isinstance(data, dict) else []
        return [v for v in variants if isinstance(v, str)]

    @staticmethod
    def _compiles(code: str) -> bool:
        try:
            compile(code, "<genesis_variant>", "exec")
            return True
        except SyntaxError:
            return False

    # ---- 业务填料: PRD 审批通知 (NotificationCenter) ------------------------

    def _default_notify(self, payload: dict) -> None:
        """PRD 升级申请 → 通知中心 HITL_REQUIRED (前端可 approve/reject)."""
        try:
            global_notifier.push(
                type="HITL_REQUIRED",
                title=f"达尔文升级申请: {payload.get('operator_name', payload.get('operator_id', '?'))}",
                content=(
                    f"算子夏普 {payload.get('old_sharpe')} → {payload.get('new_sharpe')}, "
                    f"详见 PRD: {payload.get('prd_path')}"
                ),
                payload=payload,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("notify failed: %s", exc)

    # ---- 引擎门面 (委托主库) ------------------------------------------------

    def register_operator(self, code: str, name: str | None = None) -> str:
        return self._engine.register_operator(code, name)

    def list_operators(self) -> list[dict[str, Any]]:
        return self._engine.list_operators()

    def get_operator(self, op_id: str) -> dict[str, Any] | None:
        return self._engine.get_operator(op_id)

    def get_prd(self, op_id: str) -> str | None:
        return self._engine.get_prd(op_id)

    def record_shadow(self, op_id: str, *, slippage: float, accuracy: float, extra: dict | None = None) -> dict:
        return self._engine.record_shadow(op_id, slippage=slippage, accuracy=accuracy, extra=extra)

    async def evolve(self, op_id: str, *, force: bool = False) -> dict[str, Any]:
        return await self._engine.evolve(op_id, force=force)

    def promote(self, op_id: str) -> dict[str, Any]:
        return self._engine.promote(op_id)

    def health(self) -> dict[str, Any]:
        return self._engine.health()


# 网关级单例 (与 automata / quant_coprocessor 同模式)
darwin_evolution = VeyaDarwinEvolution()
