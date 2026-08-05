"""Veya Quant Coprocessor: 量化"控制面/数据面"分离的物理执行层。

核心哲学: 大模型绝对不能触碰千万级 Tick/K 线数据帧 —— 它只扮演
"策略表达者"与"结果解读分析师"。本模块实现交火协议的两端:

1. 元数据注入 (Blinding the LLM but keeping it informed):
   get_market_data_schema() 只把 Schema + 前 5 行喂给大模型,
   让它知道字段结构(open/high/low/close/volume), 能写对计算逻辑。

2. 时序协处理器 (Quant Coprocessor Sandbox):
   execute_strategy() 在 veya 隔离沙箱(独立子进程 + 内存/时间限制 + 网络封锁)
   中加载全量数据, 执行大模型写好的 run_strategy(df) 策略代码,
   用底层 Numpy 向量化计算夏普/回撤等指标, 只返回浓缩 JSON:
   - metrics: 总收益 / 夏普 / 最大回撤
   - echarts_data_json: 抽样 500 点的图表数据(大模型只需透传)
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from veya.platform import load as _load_3o_lib
from veya.sandbox import SandboxConfig, create_safe_executor

_load_3o_lib("obase")  # 注入 3O 主库路径(幂等), 供下方 obase 资源 import

logger = logging.getLogger("quant")

# 3O 主库 oprim 根(沙箱子进程 sys.path 注入用)
_OPRIM_LIB_ROOT = Path(__file__).resolve().parent.parent / "platform" / "3O" / "oprim"

# 图表抽样点数(降低分辨率防止前端卡顿)
_CHART_SAMPLE_POINTS = 500
# 协处理器沙箱限制
_COPROCESSOR_TIMEOUT = 120.0
_COPROCESSOR_MEMORY = 2 * 1024 * 1024 * 1024  # 2GB: 全量数据加载


# =========================================================================
# 数据层 (委托 obase.market_data 资源, §1.4 单一来源; 以下为 re-export 保 API 兼容)
# =========================================================================

from obase.market_data import (  # noqa: E402,F401 — 主库资源 re-export
    ensure_synthetic_data,
    get_market_data_schema,
    resolve_data_dir,
)

# =========================================================================
# 时序协处理器 (沙箱)
# =========================================================================

def _build_sandbox_script(
    strategy_code: str, asset_id: str, start_date: str, end_date: str, data_dir: str, oprim_root: str
) -> str:
    """组装沙箱子进程脚本: 加载全量数据 → exec 策略 → 主库指标 → 浓缩 JSON。

    策略代码契约: 必须定义 run_strategy(df) -> df,
    返回的 df 需含 daily_return 与 cum_return 列。
    指标计算委托 3O 主库原子 oprim.backtest_stat(§1.4 单一来源)。
    """
    return f"""
import json
import sys
import numpy as np
import pandas as pd

# 3O 主库原子(单一来源): 回测指标由 oprim.backtest_stat 计算
sys.path.insert(0, {oprim_root!r})
from oprim._backtest_stat import backtest_stat

# 1. 物理层高速加载全量数据 (百万级行) — 在隔离子进程内
df_full = pd.read_parquet({data_dir!r} + "/" + {asset_id!r} + ".parquet")
df = df_full.loc[{start_date!r}:{end_date!r}].copy()
if len(df) == 0:
    print(json.dumps({{"status": "error", "traceback": "date range empty: {start_date}..{end_date}"}}))
    raise SystemExit(0)

# 2. 动态注入并执行大模型写好的 3O 策略逻辑 (沙箱隔离)
local_env = {{"pd": pd, "np": np}}
try:
    exec({strategy_code!r}, local_env)
    run_strategy = local_env["run_strategy"]
    portfolio_df = run_strategy(df)
except Exception as e:
    print(json.dumps({{"status": "error", "traceback": str(e)}}))
    raise SystemExit(0)

# 3. 底层计算核心指标: 委托 oprim.backtest_stat(纯 Numpy, 大模型算不准夏普/回撤)
required = ["daily_return", "cum_return"]
missing = [c for c in required if c not in portfolio_df.columns]
if missing:
    print(json.dumps({{"status": "error", "traceback": "策略输出缺少列: " + ", ".join(missing)}}))
    raise SystemExit(0)

stats = backtest_stat(
    returns=portfolio_df["daily_return"].tolist(),
    risk_free_rate=0.0,
    periods_per_year=252,
)

# 4. 组装给前端 ECharts 使用的数据抽样 (百万点 → {_CHART_SAMPLE_POINTS} 点)
step = max(1, len(portfolio_df) // {_CHART_SAMPLE_POINTS})
sampled = portfolio_df["cum_return"].iloc[::step]

chart_data = {{
    "xAxis": sampled.index.astype(str).tolist(),
    "series": sampled.values.tolist()
}}

# 5. 返回给大模型极度浓缩的数据
payload = {{
    "status": "success",
    "rows_computed": int(len(portfolio_df)),
    "metrics": {{
        "total_return": float(stats["total_return"]),
        "sharpe_ratio": float(stats["sharpe_ratio"]),
        "max_drawdown": float(stats["max_drawdown"]),
        "win_rate": float(stats["win_rate"]),
    }},
    "echarts_data_json": chart_data
}}
print(json.dumps(payload))
"""


def _build_grid_script(
    strategy_code: str, asset_id: str, params: dict[str, Any], data_dir: str
) -> str:
    """Assemble one grid-search combination's sandbox subprocess script.

    Strategy contract (distinct from _build_sandbox_script's single-run
    run_strategy(df) -> df): must define run_strategy(df, hyper_params) -> df,
    returning a df with daily_return/cum_return columns — same metric convention,
    minus per-run chart sampling (only the winning combination needs a full
    chart, produced separately by the caller's reduce/synthesis step).
    """
    return f"""
import json
import numpy as np
import pandas as pd

df = pd.read_parquet({data_dir!r} + "/" + {asset_id!r} + ".parquet")
hyper_params = json.loads({json.dumps(params)!r})

local_env = {{"pd": pd, "np": np, "hyper_params": hyper_params}}
try:
    exec({strategy_code!r}, local_env)
    run_strategy = local_env["run_strategy"]
    portfolio_df = run_strategy(df, hyper_params)
except Exception as e:
    print(json.dumps({{"status": "error", "traceback": str(e)}}))
    raise SystemExit(0)

required = ["daily_return", "cum_return"]
missing = [c for c in required if c not in portfolio_df.columns]
if missing:
    print(json.dumps({{"status": "error", "traceback": "策略输出缺少列: " + ", ".join(missing)}}))
    raise SystemExit(0)

returns = portfolio_df["daily_return"]
sharpe = float((returns.mean() / returns.std()) * np.sqrt(252)) if returns.std() != 0 else 0.0
total_return = float(portfolio_df["cum_return"].iloc[-1] - 1)
print(json.dumps({{"status": "success", "sharpe": sharpe, "total_return": total_return}}))
"""


class QuantCoprocessor:
    """量化协处理器: 沙箱中执行策略, 只回传浓缩指标与图表数据。"""

    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = resolve_data_dir(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    async def execute_strategy(
        self,
        strategy_code: str,
        asset_id: str,
        start_date: str,
        end_date: str,
    ) -> str:
        """接收大模型的策略代码, 在隔离沙箱中对海量数据计算, 返回浓缩 JSON 字符串。"""
        logger.info("[Coprocessor] 启动海量数据回测引擎, 标的: %s", asset_id)

        # 数据存在性预检(沙箱外快速失败, 明确报错)
        data_path = self.data_dir / f"{asset_id}.parquet"
        if not data_path.exists():
            return json.dumps(
                {
                    "status": "error",
                    "traceback": f"行情数据不存在: {data_path}",
                }
            )

        script = _build_sandbox_script(
            strategy_code,
            asset_id,
            start_date,
            end_date,
            str(self.data_dir),
            str(_OPRIM_LIB_ROOT),
        )
        config = SandboxConfig(
            time_limit=_COPROCESSOR_TIMEOUT,
            memory_limit=_COPROCESSOR_MEMORY,
            network_blocked=True,  # 策略代码绝不能联网
            audit_enabled=True,
            env_extra={"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
        )
        executor = create_safe_executor(config)
        async with executor:
            result = await executor.run_script(script)

        stdout = (result.get("stdout") or "").strip()
        stderr = (result.get("stderr") or "").strip()

        if result.get("exit_code") != 0:
            return json.dumps(
                {
                    "status": "error",
                    "traceback": f"协处理器异常退出 (exit={result.get('exit_code')}): {stderr or stdout}",
                }
            )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return json.dumps(
                {
                    "status": "error",
                    "traceback": f"协处理器输出无法解析: {stdout[:500] or stderr[:500]}",
                }
            )
        # 只回传浓缩数据(metrics + 抽样图表), 全量数据永远不离开沙箱
        return json.dumps(payload, ensure_ascii=False)

    async def execute_grid_search(
        self,
        strategy_code: str,
        asset_id: str,
        param_grid: dict[str, list[Any]],
        progress_callback: Callable[[int, int, dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Expand param_grid (dict of lists) into every combination and run each
        concurrently, each in its own sandboxed subprocess. run_script() is itself
        async (asyncio.create_subprocess_exec under the hood), so concurrency here
        is a plain asyncio.as_completed over fresh executors — no ProcessPoolExecutor,
        no thread bridging. progress_callback runs on the same event loop (no
        cross-thread notifier calls needed)."""
        data_path = self.data_dir / f"{asset_id}.parquet"
        if not data_path.exists():
            raise FileNotFoundError(f"行情数据不存在: {data_path}")

        keys = list(param_grid.keys())
        combos = [
            dict(zip(keys, values, strict=True))
            for values in itertools.product(*param_grid.values())
        ]

        config = SandboxConfig(
            time_limit=_COPROCESSOR_TIMEOUT,
            memory_limit=_COPROCESSOR_MEMORY,
            network_blocked=True,
            audit_enabled=True,
            env_extra={"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
        )

        async def run_one(params: dict[str, Any]) -> dict[str, Any]:
            script = _build_grid_script(strategy_code, asset_id, params, str(self.data_dir))
            executor = create_safe_executor(config)
            async with executor:
                result = await executor.run_script(script)

            stdout = (result.get("stdout") or "").strip()
            if result.get("exit_code") != 0:
                return {"params": params, "error": (result.get("stderr") or stdout)[-2000:]}
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError:
                return {"params": params, "error": f"could not parse result: {stdout[:500]}"}
            if payload.get("status") != "success":
                return {"params": params, "error": payload.get("traceback", "unknown error")}
            return {
                "params": params,
                "sharpe": payload["sharpe"],
                "total_return": payload["total_return"],
            }

        results: list[dict[str, Any]] = []
        for coro in asyncio.as_completed([run_one(p) for p in combos]):
            res = await coro
            results.append(res)
            if progress_callback:
                progress_callback(len(results), len(combos), res)
        return results

    def get_data_dir(self) -> str:
        return str(self.data_dir)


quant_coprocessor = QuantCoprocessor()
