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


def test_small_inferera_models_retired_with_128k_pool():
    """The veya1.2-128K long-context pool is gone.

    Its candidates were Inferera catalog entries whose free quota was exhausted
    on 2026-08-30, so the alias resolved to a pool that could never answer. The
    pool, the cursor and the ``VEYA_OPENROUTER_128K_POOL`` override are all
    deleted; ``veya1.2-128K`` now falls through to the veya-free eligibility
    path in canonical_proxies.
    """
    for removed in (
        "_INFERERA_128K_MODELS",
        "_INFERERA_128K_MODEL_SET",
        "_OPENROUTER_128K_DEFAULT_POOL",
        "_openrouter_128k_pool",
        "_openrouter_128k_rr_cursor",
        "_veya12_128k_call",
    ):
        assert not hasattr(hllm, removed), f"{removed} should have been retired"

    # No dead provider can be reintroduced through any remaining pool.
    assert not any(entry["provider"] == "inferera" for entry in hllm._VEYA12_FREE_POOL)


def test_128k_alias_still_resolves_to_veya_free():
    """Retiring the pool must not retire the alias (§11)."""
    from veya.obase import canonical_proxies as cp

    for spelling in ("veya1.2-128K", "veya1.2-128k", "veya-1.2-128k"):
        resolved = cp.resolve_canonical(spelling)
        assert resolved is not None, spelling
        assert resolved.canonical == "veya-free", spelling
        assert resolved.deprecated is True, spelling


def test_veya12_free_keeps_only_verified_candidates():
    assert [(entry["provider"], entry["model"]) for entry in hllm._VEYA12_FREE_POOL] == [
        ("openrouter", "liquid/lfm-2.5-2.6b:free"),
        ("openrouter", "cohere/north-mini-code:free"),
        ("openrouter", "poolside/laguna-s-2.1:free"),
        ("openrouter", "dots-studio/dots-3-note-preview:free"),
        ("openrouter", "nvidia/nemotron-3.5-lightning:free"),
    ]
