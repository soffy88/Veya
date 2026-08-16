"""主脑本体 LLM 桥的自适应 max_tokens (防 reasoning 模型详细回答被截断)。"""

from __future__ import annotations

from server.coordinator_master import (
    _MASTER_TOK_CEILING,
    _MASTER_TOK_FLOOR,
    _adaptive_master_max_tokens,
    _last_user_len,
)


def test_last_user_len_str_and_list_and_none():
    assert _last_user_len([{"role": "user", "content": "hello"}]) == 5
    assert (
        _last_user_len(
            [{"role": "user", "content": [{"type": "text", "text": "abcd"}, {"type": "image_url"}]}]
        )
        == 4
    )
    assert _last_user_len([{"role": "system", "content": "sys"}]) == 0
    # 取最后一条 user
    assert (
        _last_user_len(
            [
                {"role": "user", "content": "old"},
                {"role": "assistant", "content": "..."},
                {"role": "user", "content": "newer"},
            ]
        )
        == 5
    )


def test_short_request_gets_floor():
    mt = _adaptive_master_max_tokens([{"role": "user", "content": "hi"}], None)
    assert mt == _MASTER_TOK_FLOOR  # 短请求 = floor (已足够, 是上限非成本)


def test_long_request_scales_up_but_capped():
    mt = _adaptive_master_max_tokens([{"role": "user", "content": "x" * 200000}], None)
    assert mt == _MASTER_TOK_CEILING  # 超长请求夹到 ceiling


def test_respects_higher_explicit_value():
    huge = _MASTER_TOK_CEILING + 5000
    mt = _adaptive_master_max_tokens([{"role": "user", "content": "hi"}], huge)
    assert mt == huge  # 调用方显式设更高 → 不下调
