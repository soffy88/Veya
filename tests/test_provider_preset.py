"""opencode 网关预设的纯函数单测 (无网络): 端点注入 + 裸 model 归一化 + key 取 env。"""

from __future__ import annotations

from server.unified_pipeline import _apply_provider_preset


def test_opencode_preset_injects_endpoint_and_bare_model(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-test-123")
    model, provider, config, endpoint = _apply_provider_preset(
        "opencode-go/deepseek-v4-flash", "opencode", None
    )
    assert endpoint == "https://opencode.ai/zen/go/v1"
    assert model == "deepseek-v4-flash"  # 前缀被剥离 → 裸 id
    assert provider == "opencode"
    assert config["providers"]["opencode"]["api_key"] == "sk-test-123"


def test_opencode_preset_keeps_bare_model_and_explicit_key(monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    cfg = {"providers": {"opencode": {"api_key": "explicit"}}}
    model, _, config, endpoint = _apply_provider_preset("deepseek-v4-flash", "opencode", cfg)
    assert model == "deepseek-v4-flash"
    assert endpoint == "https://opencode.ai/zen/go/v1"
    assert config["providers"]["opencode"]["api_key"] == "explicit"  # 不覆盖显式 key


def test_non_opencode_provider_untouched():
    model, provider, config, endpoint = _apply_provider_preset("deepseek-chat", "deepseek", None)
    assert (model, provider, endpoint) == ("deepseek-chat", "deepseek", None)
    assert config is None
