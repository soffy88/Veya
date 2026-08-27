"""Quant Coprocessor 测试 — 元数据注入 / 沙箱回测 / 浓缩 JSON / 主脑协议。"""

from __future__ import annotations

import json

import pytest

from server.coordinator_master import MASTER_SYSTEM_PROMPT
from server.quant_coprocessor import (
    QuantCoprocessor,
    ensure_synthetic_data,
    get_market_data_schema,
)
from server.tool_registry import master_tools


@pytest.fixture
def synth(tmp_path):
    """生成合成行情数据并返回 data_dir。"""
    data_dir = tmp_path / "market"
    ensure_synthetic_data("AAPL", data_dir=data_dir, days=500)
    return data_dir


# ---------------------------------------------------------------------------
# 1. 元数据注入 (Blinding the LLM but keeping it informed)
# ---------------------------------------------------------------------------


def test_get_market_data_schema_only_head(synth):
    """只注入 Schema + 前 5 行, 绝不泄露全量数据。"""
    schema = get_market_data_schema("AAPL", data_dir=synth)

    # Schema 完整
    assert "Dataset: AAPL" in schema
    for col in ("open", "high", "low", "close", "volume"):
        assert f"- {col}:" in schema
    # 前 5 行样例
    assert "Sample Data (First 5 rows):" in schema
    # 明确告知行数(但不给数据)
    assert "Total rows: 500" in schema
    assert "NOT loaded into context" in schema
    # 绝对不包含全量数据: 500 行样本只会出现 5 行的时间戳
    sample_rows = schema.split("Sample Data")[1].count("\n")
    assert sample_rows <= 8  # 表头 + 5 行 + 边界


def test_get_market_data_schema_missing_asset(tmp_path):
    with pytest.raises(FileNotFoundError, match="行情数据不存在"):
        get_market_data_schema("GHOST", data_dir=tmp_path)


# ---------------------------------------------------------------------------
# 2. 时序协处理器 (沙箱)
# ---------------------------------------------------------------------------


_MA_STRATEGY = """
def run_strategy(df):
    fast = df["close"].rolling(5).mean()
    slow = df["close"].rolling(20).mean()
    pos = (fast > slow).astype(float).shift(1).fillna(0)
    daily_return = pos * df["close"].pct_change().fillna(0)
    out = df.copy()
    out["daily_return"] = daily_return
    out["cum_return"] = (1 + daily_return).cumprod()
    return out
"""


@pytest.mark.asyncio
async def test_coprocessor_full_backtest(synth):
    """沙箱回测: 策略执行 → 浓缩指标 + 抽样图表数据。"""
    cp = QuantCoprocessor(data_dir=synth)
    out = await cp.execute_strategy(_MA_STRATEGY, "AAPL", "2022-01-01", "2023-12-31")
    payload = json.loads(out)

    assert payload["status"] == "success"
    assert payload["rows_computed"] > 0
    metrics = payload["metrics"]
    assert "total_return" in metrics
    assert "sharpe_ratio" in metrics
    assert "max_drawdown" in metrics
    assert metrics["max_drawdown"] <= 0.0  # 回撤必为负
    # 图表抽样: 点数受控(<= 501), 全量 500 行被压缩
    chart = payload["echarts_data_json"]
    assert len(chart["xAxis"]) == len(chart["series"])
    assert len(chart["series"]) <= 501
    assert chart["xAxis"][0].startswith("2022-")  # 时间轴


@pytest.mark.asyncio
async def test_coprocessor_strategy_syntax_error(synth):
    cp = QuantCoprocessor(data_dir=synth)
    out = await cp.execute_strategy(
        "def run_strategy(df):\n    return df[", "AAPL", "2022-01-01", "2023-12-31"
    )
    payload = json.loads(out)
    assert payload["status"] == "error"
    assert "traceback" in payload


@pytest.mark.asyncio
async def test_coprocessor_missing_columns(synth):
    """策略输出缺 daily_return/cum_return → 明确错误。"""
    cp = QuantCoprocessor(data_dir=synth)
    out = await cp.execute_strategy(
        "def run_strategy(df):\n    return df[['close']]\n", "AAPL", "2022-01-01", "2023-12-31"
    )
    payload = json.loads(out)
    assert payload["status"] == "error"
    assert "缺少列" in payload["traceback"]


@pytest.mark.asyncio
async def test_coprocessor_missing_asset(tmp_path):
    cp = QuantCoprocessor(data_dir=tmp_path)
    out = await cp.execute_strategy(_MA_STRATEGY, "NOPE", "2022-01-01", "2023-01-01")
    payload = json.loads(out)
    assert payload["status"] == "error"
    assert "行情数据不存在" in payload["traceback"]


@pytest.mark.asyncio
async def test_coprocessor_empty_date_range(synth):
    cp = QuantCoprocessor(data_dir=synth)
    out = await cp.execute_strategy(_MA_STRATEGY, "AAPL", "2030-01-01", "2031-01-01")
    payload = json.loads(out)
    assert payload["status"] == "error"
    assert "empty" in payload["traceback"]


# ---------------------------------------------------------------------------
# 3. 工具注册 + 主脑协议
# ---------------------------------------------------------------------------


def test_quant_tools_registered():
    names = set(master_tools.list_tools())
    assert "get_market_data_schema" in names
    assert "run_backtest_coprocessor" in names


@pytest.mark.asyncio
async def test_tool_execution_real(synth, monkeypatch):
    """注册表真实执行: schema 工具 + 协处理器工具。"""
    monkeypatch.setenv("VEYA_QUANT_DATA_DIR", str(synth))

    schema = await master_tools.execute("get_market_data_schema", {"asset_id": "AAPL"})
    assert "Columns and Types:" in schema

    out = await master_tools.execute(
        "run_backtest_coprocessor",
        {
            "strategy_code": _MA_STRATEGY,
            "asset_id": "AAPL",
            "start_date": "2022-01-01",
            "end_date": "2023-12-31",
        },
    )
    payload = json.loads(out)
    assert payload["status"] == "success"
    assert payload["metrics"]["max_drawdown"] <= 0.0


def test_system_prompt_quant_protocol():
    assert "# QUANT PROTOCOL" in MASTER_SYSTEM_PROMPT
    assert "Control Plane / Data Plane separation" in MASTER_SYSTEM_PROMPT
    assert "get_market_data_schema" in MASTER_SYSTEM_PROMPT
    assert "run_backtest_coprocessor" in MASTER_SYSTEM_PROMPT
    assert "veya-artifact" in MASTER_SYSTEM_PROMPT
    assert "You are the strategy EXPRESSER" in MASTER_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_full_loop_llm_quant_protocol(tmp_path, monkeypatch):
    """完整闭环: schema 注入 → 协处理器回测 → 研报 + artifact 输出。"""
    monkeypatch.setenv("VEYA_QUANT_DATA_DIR", str(tmp_path / "market"))
    ensure_synthetic_data("BTC", data_dir=tmp_path / "market", days=300)
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(list(messages))
        turn = len(calls)
        if turn == 1:
            return _tool_response("get_market_data_schema", {"asset_id": "BTC"})
        if turn == 2:
            return _tool_response(
                "run_backtest_coprocessor",
                {
                    "strategy_code": _MA_STRATEGY,
                    "asset_id": "BTC",
                    "start_date": "2022-01-01",
                    "end_date": "2023-12-31",
                },
            )
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "夏普比率 1.2, 最大回撤 -8%。\n\n"
                            '<veya-artifact type="react" title="回测面板">\n'
                            'const chartData = {"xAxis": ["2022-01-01"], "series": [0.01]};\n'
                            "</veya-artifact>"
                        ),
                    }
                }
            ],
            "usage": {},
        }

    from server.coordinator_master import MasterCoordinator
    from server.memory_bank import VeyaMemoryBank

    coord = MasterCoordinator(
        memory_bank=VeyaMemoryBank(storage_path=tmp_path / "m.json"),
        llm_fn=fake_llm,
        max_rounds=4,
    )
    result = await coord.chat_stream("对 BTC 做双均线回测", session_id="q1")

    assert result["status"] == "success"
    assert result["tool_calls"] == [
        {"tool": "get_market_data_schema", "status": "success"},
        {"tool": "run_backtest_coprocessor", "status": "success"},
    ]
    # 协处理器浓缩 JSON 回喂给模型
    final_tool_msg = calls[2][-1]["content"]
    assert "sharpe_ratio" in final_tool_msg
    assert "echarts_data_json" in final_tool_msg
    # 最终回答携带 artifact
    assert "<veya-artifact" in result["final_answer"]


def _tool_response(name: str, args: dict, content: str = "", tc_id: str = "call_1") -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": tc_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
