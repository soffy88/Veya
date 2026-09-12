"""NVIDIA NIM model aliases backed by the Stratum key pool."""

from __future__ import annotations

import json

import pytest

from veya import llm


def test_nvidia_nim_keys_load_stratum_file(tmp_path, monkeypatch):
    key_file = tmp_path / ".pipeline_keys.json"
    key_file.write_text(json.dumps({"one": "k1", "two": "k2", "dup": "k1"}))
    monkeypatch.setenv("VEYA_NVIDIA_NIM_KEYS_FILE", str(key_file))
    monkeypatch.delenv("NVIDIA_NIM_KEY_POOL", raising=False)
    monkeypatch.delenv("NIM_KEY_POOL", raising=False)
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    assert llm._nvidia_nim_keys() == ["k1", "k2"]


@pytest.mark.asyncio
async def test_nvidia_nim_alias_round_robins_keys(monkeypatch):
    monkeypatch.setenv("NVIDIA_NIM_KEY_POOL", "k1,k2,k3")
    llm._nvidia_nim_cursors["veya-m3-nv"] = 0
    seen = []

    async def fake_provider_call(client, provider, **kwargs):
        seen.append((provider, kwargs["model"], kwargs["endpoint"], kwargs["api_key"]))
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    monkeypatch.setattr(llm, "provider_call", fake_provider_call)
    for _ in range(3):
        result = await llm.llm_call([], model="veya-M3-nv")
        assert result["router"]["route"] == "nvidia-nim-key-rr"

    assert [item[0] for item in seen] == ["openai"] * 3
    assert [item[1] for item in seen] == ["minimaxai/minimax-m3"] * 3
    assert [item[3] for item in seen] == ["k1", "k2", "k3"]


def test_nvidia_alias_catalog_is_complete():
    assert set(llm._NVIDIA_NIM_ALIASES) == {
        "veya-m3-nv",
        "veya-deepseek-v4-flash-nv",
        "veya-qwen3.5-397b-nv",
        "veya-kimi-k2.6-nv",
        "veya-glm5.1-nv",
    }
