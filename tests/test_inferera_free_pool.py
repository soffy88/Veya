"""Inferera free-model registration for the veya1.2-free pool."""

from veya import llm as hllm


def test_inferera_provider_uses_openai_compatible_chat_endpoint():
    assert hllm._ENDPOINTS["inferera"] == "https://api.inferera.com/v1/chat/completions"
    assert hllm._API_KEY_ENV["inferera"] == "INFERERA_API_KEY"


def test_inferera_free_pool_contains_live_chat_catalog_without_image_model():
    inferera_models = [
        entry["model"] for entry in hllm._VEYA12_FREE_POOL if entry["provider"] == "inferera"
    ]
    expected_free = set(hllm._INFERERA_FREE_MODELS) - set(hllm._INFERERA_128K_MODELS)

    assert len(inferera_models) == len(expected_free) == 20
    assert set(inferera_models) == expected_free
    assert "gpt-image-2-free" not in inferera_models


def test_small_inferera_models_move_to_veya12_128k():
    moved = [
        entry["model"]
        for entry in hllm._OPENROUTER_128K_DEFAULT_POOL
        if entry["provider"] == "inferera"
    ]

    assert moved == list(hllm._INFERERA_128K_MODELS)
    assert not any(
        entry["provider"] == "inferera" and entry["model"] in moved
        for entry in hllm._VEYA12_FREE_POOL
    )


def test_veya12_free_keeps_only_live_pi_candidates():
    legacy = [entry for entry in hllm._VEYA12_FREE_POOL if entry["provider"] != "inferera"]

    assert legacy == [
        {
            "provider": "tokenrouter",
            "model": "qwen/qwen3.8-max-free",
            "endpoint": "https://api.tokenrouter.com/v1",
        },
        {
            "provider": "bai",
            "model": "deepseek-v4-flash",
            "endpoint": "https://api.b.ai/v1",
        },
    ]
