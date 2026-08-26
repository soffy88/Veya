"""veya/obase/_llm_config — LLM provider configuration & credential resolution.

Package-private helper for :mod:`veya.obase.llm` (obase self-contained base
layer, SPEC v3.0 §3.4 — no reverse deps on oprim/oskill/omodul). Holds the
provider pricing/endpoint/env tables and the (provider, model) + API-key
resolution chain. Pure config/env logic, no network I/O.

``veya.obase.llm`` re-exports every public name here so ``veya.llm.get_api_key``
and friends keep their historical import path and monkeypatch surface.
"""

from __future__ import annotations

import json
import os

# ---------------------------------------------------------------------------
# Configuration tables
# ---------------------------------------------------------------------------

# Approximate pricing in USD per 1M tokens: (input, output)
_PRICING: dict[str, tuple[float, float]] = {
    "ollama": (0.0, 0.0),
    "dashscope": (0.4, 1.2),
    "anthropic": (3.0, 15.0),
    "openai": (0.5, 1.5),
    "deepseek": (0.27, 1.1),
    "openrouter": (0.15, 0.6),
    "inferera": (0.0, 0.0),
    "moonshot": (0.2, 2.0),
    "zhipu": (0.1, 0.1),
}

_DEFAULT_MODELS: dict[str, str] = {
    "veya1.2": "veya1.2",
    "ollama": "qwen38-9b-q5",
    "dashscope": "qwen-plus",
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",
    "openrouter": "openai/gpt-4o-mini",
    "inferera": "gpt-4.1-mini-free",
    "moonshot": "moonshot-v1-8k",
    "zhipu": "glm-4-flash",
}

_ENDPOINTS: dict[str, str] = {
    "ollama": "http://127.0.0.1:11434/v1/chat/completions",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "inferera": "https://api.inferera.com/v1/chat/completions",
    "moonshot": "https://api.moonshot.cn/v1/chat/completions",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
}

_API_KEY_ENV: dict[str, str] = {
    "dashscope": "DASHSCOPE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "inferera": "INFERERA_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
    "opencode-go": "OPENCODE_API_KEY",
}

_DEFAULT_PROVIDER = "veya1.2"

# NOTE: ``_user_llm_config`` and ``get_provider_config`` intentionally live in
# the facade (:mod:`veya.obase.llm`), not here — the test suite monkeypatches
# ``veya.llm._user_llm_config`` and ``get_provider_config`` must resolve that
# patched name within the facade's namespace.


def _opencode_go_key_from_auth() -> str:
    """opencode-go key 兜底: 读 opencode 本地凭据 (auth.json)。

    宿主侧通常无 OPENCODE_API_KEY env; opencode CLI 装过即有此文件。
    """
    try:
        path = os.path.expanduser("~/.local/share/opencode/auth.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        ent = data.get("opencode-go") or {}
        return str(ent.get("key") or "")
    except Exception:
        return ""


def get_api_key(provider: str, config: dict | None = None) -> str:
    """Return the API key for a provider (env var > config dict)."""
    providers_cfg = (config or {}).get("providers") or {}
    if isinstance(providers_cfg, dict):
        key = providers_cfg.get(provider)
        if isinstance(key, dict):
            key = key.get("api_key")
        if key:
            return str(key)
    env_name = _API_KEY_ENV.get(provider, f"{provider.upper()}_API_KEY")
    key = os.environ.get(env_name, "")
    if not key and provider == "opencode-go":
        key = _opencode_go_key_from_auth()
    return key


def calc_cost(provider: str, usage: dict) -> float:
    """Approximate cost in USD for a usage dict (OpenAI or Anthropic keys)."""
    in_price, out_price = _PRICING.get(provider, (0.0, 0.0))
    in_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0
    out_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0
    return in_tokens * in_price / 1_000_000 + out_tokens * out_price / 1_000_000
