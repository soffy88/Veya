"""静态不变量校验引擎测试 — AST 硬扫描 (无 LLM, 确定性)."""

from __future__ import annotations

from server.static_invariant import VeyaStaticInvariant

check = VeyaStaticInvariant.check


# =========================================================================
# L1: 未来函数 shift(-k)
# =========================================================================

def test_block_lookahead_shift():
    """shift(-1) 进入信号列 → block."""
    r = check(
        'def run_strategy(df):\n'
        '    df["signal"] = df["close"].shift(-1) / df["close"] - 1\n'
        '    return df\n'
    )
    assert r["verdict"] == "block"
    assert r["summary"]["lookahead_shifts"] == 1
    assert r["violations"][0]["rule_id"] == "L1"


def test_block_shift_negative_k():
    """shift(-5) 同样拦截."""
    r = check('df["signal"] = df["close"].shift(-5)\n')
    assert r["verdict"] == "block"


def test_label_column_downgraded():
    """shift(-1) 仅用于构造监督标签列 → 降级 WARNING (不硬拦截)."""
    r = check(
        'def run_strategy(df):\n'
        '    df["target"] = df["close"].shift(-1) / df["close"] - 1\n'
        '    df["signal"] = df["close"].rolling(10).mean().shift(1)\n'
        '    return df\n'
    )
    assert r["verdict"] == "review"
    assert r["summary"]["violations"] == 0
    assert r["summary"]["warnings"] == 1


# =========================================================================
# L2: 未来行索引
# =========================================================================

def test_block_future_index_loop():
    """循环内 df.iloc[i+1] → block."""
    r = check(
        'def run_strategy(df):\n'
        '    for i in range(len(df)):\n'
        '        x = df["close"].iloc[i+1]\n'
        '    return df\n'
    )
    assert r["verdict"] == "block"
    assert r["violations"][0]["rule_id"] == "L2"


def test_block_future_slice_lower_bound():
    """df.loc[i+1:] 未来切片 → block."""
    r = check('y = df.loc[i+1:]\n')
    assert r["verdict"] == "block"


# =========================================================================
# L3: np.roll 负偏移
# =========================================================================

def test_block_np_roll_negative():
    r = check('import numpy as np\ny = np.roll(df["close"].values, -1)\n')
    assert r["verdict"] == "block"
    assert r["violations"][0]["rule_id"] == "L3"


# =========================================================================
# L4: 滚动统计量泄漏
# =========================================================================

def test_warning_rolling_without_shift():
    """rolling 统计量未滞后即参与决策 → review (WARNING)."""
    r = check(
        'def run_strategy(df):\n'
        '    df["ma"] = df["close"].rolling(20).mean()\n'
        '    df["signal"] = df["close"] > df["ma"]\n'
        '    return df\n'
    )
    assert r["verdict"] == "review"
    assert r["summary"]["leakage_risks"] == 1


def test_pass_rolling_with_shift():
    """rolling 统计量已 shift(1) 滞后 → pass."""
    r = check(
        'def run_strategy(df):\n'
        '    df["ma"] = df["close"].rolling(20).mean().shift(1)\n'
        '    df["signal"] = df["close"] > df["ma"]\n'
        '    return df\n'
    )
    assert r["verdict"] == "pass"


# =========================================================================
# L5: volume 分母除零
# =========================================================================

def test_warning_vwap_div_zero():
    """VWAP 类 volume 分母 → WARNING."""
    r = check(
        'df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()\n'
    )
    assert r["verdict"] == "review"
    assert r["summary"]["div_zero_risks"] == 1


# =========================================================================
# 边界
# =========================================================================

def test_syntax_error_blocked():
    """语法错误 → block (syntax finding)."""
    r = check("def run_strategy(df:\n")
    assert r["verdict"] == "block"
    assert r["violations"][0]["rule_id"] == "syntax"


def test_clean_strategy_passes():
    """教科书合规策略 → pass."""
    r = check(
        'def run_strategy(df):\n'
        '    df["ma"] = df["close"].rolling(20).mean().shift(1)\n'
        '    df["signal"] = (df["close"] > df["ma"]).astype(int)\n'
        '    df["daily_return"] = df["close"].pct_change().fillna(0)\n'
        '    df["cum_return"] = (1 + df["daily_return"]).cumprod()\n'
        '    return df\n'
    )
    assert r["verdict"] == "pass"
    assert r["findings"] == []


def test_findings_carry_line_snippet():
    """findings 带行号与源码摘录 (供红队引用)."""
    r = check('df["signal"] = df["close"].shift(-1)\n')
    f = r["violations"][0]
    assert f["line"] == 1
    assert "shift(-1)" in f["snippet"]
