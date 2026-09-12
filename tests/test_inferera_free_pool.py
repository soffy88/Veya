"""Inferera free-model registration for the veya1.2-free pool."""

from veya import llm as hllm


def test_inferera_provider_uses_openai_compatible_chat_endpoint():
    assert hllm._ENDPOINTS["inferera"] == "https://api.inferera.com/v1/chat/completions"
    assert hllm._API_KEY_ENV["inferera"] == "INFERERA_API_KEY"


def test_inferera_free_pool_excludes_depleted_models():
    inferera_models = [
        entry["model"] for entry in hllm._VEYA12_FREE_POOL if entry["provider"] == "inferera"
    ]

    assert hllm._INFERERA_FREE_MODELS == ()
    assert inferera_models == []
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


def test_veya12_free_keeps_only_verified_candidates():
    assert [(entry["provider"], entry["model"]) for entry in hllm._VEYA12_FREE_POOL] == [
        ("openai", "opencode-go/nemotron-3.5-lightning-free"),
        ("gmi-serving", "MiniMaxAI/MiniMax-M3"),
        ("bai", "deepseek-v4-flash"),
        ("bai", "hy3"),
        ("bai", "qwen3.8-flash"),
        ("bai", "deepseek-v4-flash-vision-exp"),
    ]
