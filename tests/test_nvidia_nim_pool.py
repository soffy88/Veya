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
async def test_nim_proxy_round_robins_keys(monkeypatch, tmp_path):
    """SPEC v1.0 §8.2: the veya-nim proxy round-robins its dedicated key pool.

    Replaces the old ``test_nvidia_nim_alias_round_robins_keys``, which asserted
    that ``veya-M3-nv`` binds to ``minimaxai/minimax-m3`` — an upstream model
    that has been 410 EOL since 2026-09-09. The alias now remaps to the veya-nim
    proxy and selects only from the eligibility-filtered pool (§5).
    """
    from veya.obase import canonical_proxies as cp

    state = tmp_path / "model-state.json"
    state.write_text(
        json.dumps(
            {
                "models": {
                    "nvidia-nim:z-ai/glm-5.3": {
                        "provider": "nvidia-nim",
                        "model_id": "z-ai/glm-5.3",
                        "canonical_proxy": "veya-nim",
                        "eligible": True,
                        "discovered": True,
                        "healthy": True,
                        "credentials_valid": True,
                        "endpoint_available": True,
                        "model_available": True,
                        "cooldown_until": None,
                        "latency_ms": 100,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cp, "_MODEL_STATE", state)
    monkeypatch.setattr(cp, "_NIM_KEY_POOL", tmp_path / "nim-key-pool")
    (tmp_path / "nim-key-pool").write_text("k1\nk2\nk3\n", encoding="utf-8")
    cp.reset_nim_key_pool()
    seen: list[tuple] = []

    async def fake_provider_call(client, provider, **kwargs):
        seen.append((provider, kwargs["model"], kwargs["endpoint"], kwargs["api_key"]))
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    monkeypatch.setattr(llm, "provider_call", fake_provider_call)
    cp._nim_cursor = 0

    for _ in range(3):
        result = await llm.llm_call([], model="veya-M3-nv")
        # legacy consumers still find a route label
        assert result["router"]["route"] == "canonical-veya-nim"
        # canonical contract: the retired upstream id is never selected
        assert result["router"]["RESOLVED_UPSTREAM_MODEL"] == "z-ai/glm-5.3"
        assert result["router"]["ROUTED_PROXY"] == "veya-nim"

    assert [item[0] for item in seen] == ["openai"] * 3
    assert [item[1] for item in seen] == ["z-ai/glm-5.3"] * 3
    assert "minimaxai/minimax-m3" not in [item[1] for item in seen]
    assert [item[3] for item in seen] == ["k1", "k2", "k3"]
    cp.reset_nim_key_pool()


@pytest.mark.asyncio
async def test_nim_alias_substitution_is_visible(monkeypatch, tmp_path):
    """SPEC v1.0 §11: legacy alias remap must be visible, never silent."""
    from veya.obase import canonical_proxies as cp

    state = tmp_path / "model-state.json"
    state.write_text(
        json.dumps(
            {
                "models": {
                    "nvidia-nim:z-ai/glm-5.3": {
                        "provider": "nvidia-nim",
                        "model_id": "z-ai/glm-5.3",
                        "canonical_proxy": "veya-nim",
                        "eligible": True,
                        "discovered": True,
                        "healthy": True,
                        "credentials_valid": True,
                        "endpoint_available": True,
                        "model_available": True,
                        "cooldown_until": None,
                        "latency_ms": 100,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cp, "_MODEL_STATE", state)
    monkeypatch.setattr(cp, "_NIM_KEY_POOL", tmp_path / "nim-key-pool")
    (tmp_path / "nim-key-pool").write_text("k1\n", encoding="utf-8")
    cp.reset_nim_key_pool()
    cp._nim_cursor = 0

    async def fake_provider_call(client, provider, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    monkeypatch.setattr(llm, "provider_call", fake_provider_call)
    result = await llm.llm_call([], model="veya-m3-nv")
    dep = result["router"]["DEPRECATION"]
    assert result["router"]["REQUESTED_PROXY"] == "veya-m3-nv"
    assert dep["canonical_proxy"] == "veya-nim"
    assert dep["visible"] is True
    assert dep["silent_substitution"] is False
    assert "410 EOL" in dep["retired_upstream"]
    cp.reset_nim_key_pool()


def test_nvidia_alias_catalog_is_complete():
    assert set(llm._NVIDIA_NIM_ALIASES) == {
        "veya-m3-nv",
        "veya-deepseek-v4-flash-nv",
        "veya-qwen3.5-397b-nv",
        "veya-kimi-k2.6-nv",
        "veya-glm5.1-nv",
    }
