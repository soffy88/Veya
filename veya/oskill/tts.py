"""
veya/oskill/tts.py — Text-to-Speech pipeline (Layer 2).

Composite skill built on oprim audio ops + provider TTS APIs.
Handles: text preprocessing → provider call → audio streaming.

Supports providers: openai (tts-1), dashscope (cosyvoice), elevenlabs.
"""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from typing import Any

try:
    import httpx

    _HAS_HTTPX = True
except ImportError:
    httpx = None  # type: ignore
    _HAS_HTTPX = False


# ---------------------------------------------------------------------------
# Provider endpoints & config
# ---------------------------------------------------------------------------

_TTS_PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "endpoint": "https://api.openai.com/v1/audio/speech",
        "api_key_env": "OPENAI_API_KEY",
        "default_model": "tts-1",
        "default_voice": "alloy",
        "voices": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"],
    },
    "dashscope": {
        "endpoint": "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/speech-synthesizer",
        "api_key_env": "DASHSCOPE_API_KEY",
        "default_model": "cosyvoice-v1",
        "default_voice": "longxiaochun",
        "voices": ["longxiaochun", "longxiaoxia", "longcheng"],
    },
    "elevenlabs": {
        "endpoint": "https://api.elevenlabs.io/v1/text-to-speech",
        "api_key_env": "ELEVENLABS_API_KEY",
        "default_model": "eleven_multilingual_v2",
        "default_voice": "21m00Tcm4TlvDq8ikWAM",  # Rachel
        "voices": [],
    },
}


def _get_tts_config(provider: str) -> dict | None:
    return _TTS_PROVIDERS.get(provider.lower())


def _get_api_key(provider: str) -> str:
    cfg = _TTS_PROVIDERS.get(provider.lower(), {})
    env_var = cfg.get("api_key_env", f"{provider.upper()}_API_KEY")
    return os.environ.get(env_var, "")


# ---------------------------------------------------------------------------
# Core TTS functions
# ---------------------------------------------------------------------------


async def text_to_speech(
    text: str,
    provider: str = "openai",
    *,
    model: str | None = None,
    voice: str | None = None,
    speed: float = 1.0,
    format: str = "mp3",
    timeout: float = 60.0,
) -> bytes:
    """Synthesize speech from text. Returns complete audio bytes."""
    if not _HAS_HTTPX:
        raise RuntimeError("httpx not installed — install with: pip install httpx")
    cfg = _get_tts_config(provider)
    if cfg is None:
        raise ValueError(f"Unknown TTS provider: {provider}")

    api_key = _get_api_key(provider)
    if not api_key:
        raise ValueError(f"API key not configured for {provider}")

    resolved_model = model or cfg.get("default_model", "tts-1")
    resolved_voice = voice or cfg.get("default_voice", "alloy")

    if provider == "openai":
        return await _tts_openai(
            text,
            api_key,
            cfg["endpoint"],
            resolved_model,
            resolved_voice,
            speed,
            format,
            timeout,
        )
    elif provider == "elevenlabs":
        return await _tts_elevenlabs(
            text,
            api_key,
            cfg["endpoint"],
            resolved_model,
            resolved_voice,
            timeout,
        )
    elif provider == "dashscope":
        return await _tts_dashscope(
            text,
            api_key,
            cfg["endpoint"],
            resolved_model,
            resolved_voice,
            speed,
            format,
            timeout,
        )
    else:
        raise ValueError(f"Provider {provider} not implemented")


async def _tts_openai(
    text: str,
    api_key: str,
    endpoint: str,
    model: str,
    voice: str,
    speed: float,
    fmt: str,
    timeout: float,
) -> bytes:
    """Call OpenAI TTS API."""
    body = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": fmt,
        "speed": speed,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        return resp.content


async def _tts_elevenlabs(
    text: str,
    api_key: str,
    endpoint: str,
    model: str,
    voice: str,
    timeout: float,
) -> bytes:
    """Call ElevenLabs TTS API."""
    url = f"{endpoint}/{voice}"
    body = {
        "text": text,
        "model_id": model,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json=body,
        )
        resp.raise_for_status()
        return resp.content


async def _tts_dashscope(
    text: str,
    api_key: str,
    endpoint: str,
    model: str,
    voice: str,
    speed: float,
    fmt: str,
    timeout: float,
) -> bytes:
    """Call DashScope CosyVoice TTS API."""
    body = {
        "model": model,
        "input": {"text": text},
        "parameters": {
            "voice": voice,
            "speech_rate": int(speed * 100),
            "format": fmt,
        },
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        # DashScope returns base64-encoded audio
        audio_b64 = data.get("output", {}).get("audio", {}).get("data", "")
        if audio_b64:
            return base64.b64decode(audio_b64)
        return b""


# ---------------------------------------------------------------------------
# Streaming TTS (chunked audio output)
# ---------------------------------------------------------------------------


async def text_to_speech_streaming(
    text: str,
    provider: str = "openai",
    *,
    model: str | None = None,
    voice: str | None = None,
    format: str = "pcm",
    timeout: float = 60.0,
) -> AsyncIterator[bytes]:
    """Stream TTS audio chunks as they're generated.

    Note: OpenAI TTS does not support true streaming; this returns the full
    audio in one chunk. ElevenLabs supports streaming via WebSocket.

    Args:
        text: Text to synthesize.
        provider: TTS provider.
        model: Model name.
        voice: Voice ID.
        format: Output format.
        timeout: Timeout.

    Yields:
        Audio byte chunks.

    Example:
        >>> async for chunk in text_to_speech_streaming("Hello!", provider="openai"):
        ...     player.write(chunk)
    """
    # For providers without native streaming, we synthesize and yield in chunks
    full_audio = await text_to_speech(
        text,
        provider=provider,
        model=model,
        voice=voice,
        format=format,
        timeout=timeout,
    )

    # Simulate streaming by yielding in ~4KB chunks
    chunk_size = 4096
    for i in range(0, len(full_audio), chunk_size):
        yield full_audio[i : i + chunk_size]


# ---------------------------------------------------------------------------
# Voice list
# ---------------------------------------------------------------------------


async def list_voices(
    provider: str = "openai",
    timeout: float = 30.0,
) -> list[dict[str, str]]:
    """List available voices for a TTS provider.

    Returns:
        List of {"id": ..., "name": ...} dicts.

    Example:
        >>> voices = await list_voices("openai")
        >>> for v in voices:
        ...     print(v["name"])
    """
    cfg = _get_tts_config(provider)
    if cfg is None:
        return []

    voices = cfg.get("voices", [])
    if voices:
        return [{"id": v, "name": v} for v in voices]

    # For elevenlabs, fetch dynamically
    if provider == "elevenlabs":
        api_key = _get_api_key(provider)
        if not api_key:
            return []
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                resp = await client.get(
                    "https://api.elevenlabs.io/v1/voices",
                    headers={"xi-api-key": api_key},
                )
                resp.raise_for_status()
                data = resp.json()
                return [
                    {"id": v.get("voice_id", ""), "name": v.get("name", "")}
                    for v in data.get("voices", [])
                ]
            except Exception:
                return []

    return []
