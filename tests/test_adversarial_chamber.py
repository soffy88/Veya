"""红蓝对抗审判庭测试 — 确定性模式 (无 LLM) + 注入 LLM 模式."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.adversarial_chamber import VeyaAdversarialChamber

BAD_CODE = (
    'import pandas as pd\n'
    'def run_strategy(df):\n'
    '    df["signal"] = df["close"].shift(-1) / df["close"] - 1\n'
    '    df["ma"] = df["close"].rolling(20).mean()\n'
    '    df["daily_return"] = df["close"].pct_change().fillna(0)\n'
    '    df["cum_return"] = (1 + df["daily_return"]).cumprod()\n'
    '    return df\n'
)

GOOD_CODE = (
    'def run_strategy(df):\n'
    '    df["ma"] = df["close"].rolling(20).mean().shift(1)\n'
    '    df["signal"] = (df["close"] > df["ma"]).astype(int)\n'
    '    df["daily_return"] = df["close"].pct_change().fillna(0)\n'
    '    df["cum_return"] = (1 + df["daily_return"]).cumprod()\n'
    '    return df\n'
)


@pytest.fixture
def chamber(tmp_path) -> VeyaAdversarialChamber:
    return VeyaAdversarialChamber(output_dir=str(tmp_path / "reports"))


# =========================================================================
# 确定性模式 (离线安全, 可复现)
# =========================================================================

async def test_blocked_strategy_with_lookahead(chamber):
    """偷价策略 → blocked, 红队点出未来函数, 分数不达标."""
    r = await chamber.review(BAD_CODE, strategy_name="vwap_breakout")

    assert r["status"] == "blocked"
    assert r["violations"] == 1
    assert r["red_points"] >= 1
    assert r["safety_score_before"] == 60.0
    assert r["safety_score_after"] == 60.0  # 硬违规未修复不加分
    assert "shift(-1)" in r["final_code"]  # 修正头注入 + 原码保留
    assert r["final_code"].startswith("# ===== 红蓝对抗审判庭修正头")
    assert r["fingerprint"]


async def test_approved_clean_strategy(chamber):
    """合规策略 → approved, 蓝队加固加分."""
    r = await chamber.review(GOOD_CODE, strategy_name="ma_cross")

    assert r["status"] == "approved"
    assert r["violations"] == 0
    assert r["safety_score_after"] >= r["safety_score_before"]
    assert r["safety_score_after"] == 100.0
    assert r["judge_fixes"] == []


async def test_report_markdown_written(chamber):
    """《红蓝对抗审计报告》落盘, 含双方辩论与裁决."""
    r = await chamber.review(GOOD_CODE, strategy_name="ma_cross", context="BTC 1h")
    report = Path(r["report_path"])
    assert report.exists()
    md = report.read_text(encoding="utf-8")
    assert "红蓝对抗审计报告" in md
    assert "蓝队辩护" in md
    assert "红队质疑" in md
    assert "主脑裁决" in md
    assert "BTC" in md


async def test_needs_review_on_warnings_only(chamber):
    """仅 WARNING (如 rolling 未滞后) → needs_review 或 approved, 但非 blocked."""
    code = GOOD_CODE.replace('df["close"].rolling(20).mean().shift(1)',
                             'df["close"].rolling(20).mean()')
    r = await chamber.review(code, strategy_name="leaky_ma")
    assert r["status"] in ("needs_review", "approved")
    assert r["violations"] == 0
    assert r["warnings"] == 1
    assert r["safety_score_after"] >= 70.0


# =========================================================================
# LLM 注入模式 (fake caller, 验证辩论调用链)
# =========================================================================

async def test_llm_mode_uses_injected_caller(tmp_path):
    """注入 fake LLM → 蓝/红/主脑三轮调用, 报告含模型输出."""

    calls: list[str] = []

    async def fake_llm(messages, tools=None, max_tokens=4096, **kw):
        prompt = messages[0]["content"]
        calls.append(prompt)
        if "你是蓝队" in prompt:
            text = "蓝队: 盈利核心是动量延续 + 波动率过滤."
        elif "你是红队" in prompt:
            text = '{"points": [{"id": "R1", "severity": "high", "title": "偷价", "detail": "shift(-1)", "line": 2}], "summary": "存在未来函数"}'
        else:
            text = '{"verdict": "needs_review", "safety_score": 78, "fixes": [{"target": "L2", "action": "改用 shift(1)"}], "final_code": "def run_strategy(df):\\n    return df"}'
        return {"content": text, "usage": {"input_tokens": 10, "output_tokens": 5}}

    chamber = VeyaAdversarialChamber(llm_fn=fake_llm, output_dir=str(tmp_path / "reports"))
    r = await chamber.review(BAD_CODE, strategy_name="llm_trial")

    assert len(calls) == 3  # 蓝队 + 红队 + 主脑
    assert r["status"] == "needs_review"
    assert r["safety_score_after"] == 78
    assert r["final_code"].startswith("def run_strategy")
    # 主脑修正项来自 LLM
    assert r["judge_fixes"][0]["action"] == "改用 shift(1)"
    # 报告包含模型生成的裁决理由
    md = Path(r["report_path"]).read_text(encoding="utf-8")
    assert "needs_review" in md
