"""Read-only Open-LLM-VTuber (OLV) Phase 0 bridge.

OLV is a standalone voice + Live2D frontend that runs as a separate process and
talks to Veya only through the existing OpenAI-compatible LLM gateway
(``scripts/veya_llm_gateway.py``, default port 8791).  This module is the whole
Veya-side integration for Phase 0: it answers *how to point OLV at Veya* and
nothing else.

Hard boundaries (docs/open-llm-vtuber-integration.md §7.2 / §7.5):

- no process spawn, no port, no tool registration, no persistent state;
- no import of OLV code and no OLV dependency in ``pyproject.toml``;
- no Live2D model asset is vendored, copied or referenced by path;
- OLV's MCP client and memory are deliberately not wired — they would form a
  second tool/memory authority next to Veya's;
- with the flag off, :func:`status` returns exactly ``{"enabled": False}`` and
  the tool surface stays byte-identical to the frozen mainline.

Tokens are never produced, stored or echoed here: the OLV-side ``conf.yaml``
holds the issued gateway token, Veya only documents how to issue one.
"""

from __future__ import annotations

import os
from typing import Any

from server.feature_flags import enabled as _flag_enabled

FLAG_NAME = "VEYA_OLV"
HOST_ENV = "VEYA_OLV_HOST"
PORT_ENV = "VEYA_OLV_GATEWAY_PORT"
MODEL_ENV = "VEYA_OLV_MODEL"

DEFAULT_HOST = "127.0.0.1"
# Matches scripts/veya_llm_gateway.py's documented default port.
DEFAULT_PORT = 8791
DEFAULT_MODEL = "veya1.2"

# Live2D expression keys OLV injects into the system prompt
# (prompts/live2d_expression_prompt + model_dict.json emotionMap).  A Veya-side
# brain that never receives the prompt produces lip-sync without expressions, so
# the template keeps the prompt enabled and lists the accepted keys.
EMOTION_KEYS: tuple[str, ...] = (
    "neutral",
    "joy",
    "smirk",
    "sadness",
    "anger",
    "disgust",
    "fear",
    "surprise",
)

_TOKEN_HINT = "<issued token>"


def enabled() -> bool:
    """Whether the opt-in bridge is enabled through the canonical flag."""
    try:
        return _flag_enabled(FLAG_NAME)
    except KeyError:
        return False


def gateway_host() -> str:
    """Host OLV should use to reach the Veya gateway."""
    return os.environ.get(HOST_ENV, "").strip() or DEFAULT_HOST


def gateway_port() -> int:
    """Port of the Veya OpenAI-compatible gateway."""
    raw = os.environ.get(PORT_ENV, "").strip()
    if raw.isdigit() and 1 <= int(raw) <= 65535:
        return int(raw)
    return DEFAULT_PORT


def gateway_model() -> str:
    """Model name handed to OLV for the Veya gateway."""
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def _urls() -> dict[str, str]:
    base = f"http://{gateway_host()}:{gateway_port()}/v1"
    return {
        "gateway_url": base,
        "chat_completions_url": f"{base}/chat/completions",
        "models_url": f"{base}/models",
        "health_url": f"http://{gateway_host()}:{gateway_port()}/health",
    }


def status() -> dict[str, Any]:
    """Read-only availability projection; never spawns or registers anything."""
    if not enabled():
        return {"enabled": False}
    return {
        "enabled": True,
        "flag": FLAG_NAME,
        "token_required": True,
        "model": gateway_model(),
        "emotion_keys": list(EMOTION_KEYS),
        **_urls(),
    }


def _persona_prompt() -> str:
    return (
        "你是屏幕里的虚拟形象，用简短口语的中文回答，一次不超过两句。"
        "不要输出 Markdown、代码块、列表或链接；不要复述用户的提问。"
        "在句首或句尾用一个表情关键字表达当前情绪，只能是：" + "、".join(EMOTION_KEYS) + "。"
    )


def config_template() -> dict[str, Any]:
    """The OLV ``conf.yaml`` fragment that points OLV's LLM at the Veya gateway.

    The returned mapping carries no credential: ``llm_api_key`` stays a
    placeholder so nothing sensitive can leak through this module.
    """
    if not enabled():
        return {"enabled": False}
    base = _urls()["gateway_url"]
    return {
        "enabled": True,
        "llm": {
            "provider": "openai_compatible_llm",
            "base_url": base,
            "llm_api_key": _TOKEN_HINT,
            "model": gateway_model(),
        },
        "agent": {
            "conversation_agent_choice": "basic_memory_agent",
            "use_mcpp": False,
            "faster_first_response": True,
            "segment_method": "pysbd",
        },
        "asr": {"asr_model": "sherpa_onnx_asr"},
        "tts": {"tts_model": "edge_tts"},
        "live2d_expression_prompt": True,
        "emotion_keys": list(EMOTION_KEYS),
        "persona_prompt": _persona_prompt(),
        "token_issue_command": (
            "python -m scripts.veya_llm_gateway_tokens issue --name open-llm-vtuber --ttl-days 30"
        ),
    }


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_MODEL",
    "DEFAULT_PORT",
    "EMOTION_KEYS",
    "FLAG_NAME",
    "HOST_ENV",
    "MODEL_ENV",
    "PORT_ENV",
    "config_template",
    "enabled",
    "gateway_host",
    "gateway_model",
    "gateway_port",
    "status",
]
