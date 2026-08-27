"""
veya/oskill/stt.py — Speech-to-Text pipeline (Layer 2).

Composite skill built on oprim audio ops + provider STT APIs.
Handles: audio prep → provider call → transcription result formatting.

Supports providers: openai (whisper), dashscope (paraformer), deepgram.
"""

from __future__ import annotations

import os
from typing import Any

try:
    import httpx

    _HAS_HTTPX = True
except ImportError:
    httpx = None  # type: ignore
    _HAS_HTTPX = False

from veya.oprim.audio import pcm_to_wav
from veya.oprim.types import (
    TranscriptionResult,
    TranscriptionWord,
)

# ---------------------------------------------------------------------------
# Provider endpoints & config
# ---------------------------------------------------------------------------

_STT_PROVIDERS: dict[str, dict[str, str]] = {
    "openai": {
        "endpoint": "https://api.openai.com/v1/audio/transcriptions",
        "api_key_env": "OPENAI_API_KEY",
        "default_model": "whisper-1",
    },
    "dashscope": {
        "endpoint": "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription",
        "api_key_env": "DASHSCOPE_API_KEY",
        "default_model": "paraformer-v2",
    },
    "deepgram": {
        "endpoint": "https://api.deepgram.com/v1/listen",
        "api_key_env": "DEEPGRAM_API_KEY",
        "default_model": "nova-2",
    },
}


def _get_stt_config(provider: str) -> dict | None:
    """Resolve STT provider configuration."""
    return _STT_PROVIDERS.get(provider.lower())


def _get_api_key(provider: str) -> str:
    """Get API key for an STT provider."""
    cfg = _STT_PROVIDERS.get(provider.lower(), {})
    env_var = cfg.get("api_key_env", f"{provider.upper()}_API_KEY")
    return os.environ.get(env_var, "")


# ---------------------------------------------------------------------------
# Core STT functions
# ---------------------------------------------------------------------------


async def speech_to_text(
    audio: bytes,
    provider: str = "openai",
    *,
    model: str | None = None,
    language: str | None = None,
    prompt: str | None = None,
    sample_rate: int = 16000,
    response_format: str = "verbose_json",
    timeout: float = 120.0,
) -> TranscriptionResult:
    """Transcribe speech audio to text."""
    if not _HAS_HTTPX:
        return TranscriptionResult(text="", metadata={"error": "httpx not installed"})
    cfg = _get_stt_config(provider)
    if cfg is None:
        return TranscriptionResult(
            text="",
            metadata={"error": f"Unknown STT provider: {provider}"},
        )

    api_key = _get_api_key(provider)
    if not api_key:
        return TranscriptionResult(
            text="",
            metadata={"error": f"API key not configured for {provider}"},
        )

    resolved_model = model or cfg.get("default_model", "whisper-1")
    endpoint = cfg["endpoint"]

    # Wrap PCM as WAV (most STT APIs expect a container format)
    # Detect if already WAV by checking RIFF header
    audio_file_data = audio if audio[:4] == b"RIFF" else pcm_to_wav(audio, sample_rate=sample_rate)

    if provider == "openai":
        return await _stt_openai(
            audio_file_data,
            api_key,
            endpoint,
            resolved_model,
            language,
            prompt,
            response_format,
            timeout,
        )
    elif provider == "deepgram":
        return await _stt_deepgram(
            audio_file_data,
            api_key,
            endpoint,
            resolved_model,
            language,
            timeout,
        )
    elif provider == "dashscope":
        return await _stt_dashscope(
            audio_file_data,
            api_key,
            endpoint,
            resolved_model,
            language,
            timeout,
        )
    else:
        return TranscriptionResult(
            text="",
            metadata={"error": f"Provider {provider} not implemented"},
        )


async def _stt_openai(
    audio_data: bytes,
    api_key: str,
    endpoint: str,
    model: str,
    language: str | None,
    prompt: str | None,
    response_format: str,
    timeout: float,
) -> TranscriptionResult:
    """Call OpenAI Whisper API."""
    form_data: dict[str, Any] = {
        "model": model,
        "response_format": response_format,
    }
    if language:
        form_data["language"] = language
    if prompt:
        form_data["prompt"] = prompt

    files = {
        "file": ("audio.wav", audio_data, "audio/wav"),
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                data=form_data,
                files=files,
            )
            resp.raise_for_status()
            data = resp.json()

            if response_format == "verbose_json":
                words = [
                    TranscriptionWord(
                        text=w.get("word", ""),
                        start_ms=w.get("start", 0) * 1000,
                        end_ms=w.get("end", 0) * 1000,
                        confidence=w.get("confidence", 1.0),
                    )
                    for w in data.get("words", [])
                ]
                return TranscriptionResult(
                    text=data.get("text", ""),
                    words=words,
                    language=data.get("language", language or ""),
                    confidence=data.get("confidence", 1.0),
                    duration_ms=data.get("duration", 0) * 1000,
                )
            else:
                # Simple text response
                text = data.get("text", "") if isinstance(data, dict) else str(data)
                return TranscriptionResult(text=text.strip())
        except Exception as e:
            return TranscriptionResult(
                text="",
                metadata={"error": str(e)},
            )


async def _stt_deepgram(
    audio_data: bytes,
    api_key: str,
    endpoint: str,
    model: str,
    language: str | None,
    timeout: float,
) -> TranscriptionResult:
    """Call Deepgram API."""
    params: dict[str, str] = {"model": model}
    if language:
        params["language"] = language

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Token {api_key}",
                    "Content-Type": "audio/wav",
                },
                params=params,
                content=audio_data,
            )
            resp.raise_for_status()
            data = resp.json()

            channel = (data.get("results", {}).get("channels", [{}]) or [{}])[0]
            alternatives = channel.get("alternatives", [{}])
            best = alternatives[0] if alternatives else {}

            words = [
                TranscriptionWord(
                    text=w.get("word", ""),
                    start_ms=w.get("start", 0) * 1000,
                    end_ms=w.get("end", 0) * 1000,
                    confidence=w.get("confidence", 1.0),
                )
                for w in best.get("words", [])
            ]
            return TranscriptionResult(
                text=best.get("transcript", ""),
                words=words,
                confidence=best.get("confidence", 1.0),
                duration_ms=data.get("metadata", {}).get("duration", 0) * 1000,
            )
        except Exception as e:
            return TranscriptionResult(text="", metadata={"error": str(e)})


async def _stt_dashscope(
    audio_data: bytes,
    api_key: str,
    endpoint: str,
    model: str,
    language: str | None,
    timeout: float,
) -> TranscriptionResult:
    """Call DashScope (Aliyun) Paraformer ASR API."""
    # DashScope accepts file URL or base64; we use a simple approach
    import base64

    b64_audio = base64.b64encode(audio_data).decode("utf-8")

    body = {
        "model": model,
        "input": {"audio": b64_audio},
        "parameters": {},
    }
    if language:
        body["parameters"]["language"] = language

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
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

            output = data.get("output", {})
            text = output.get("text", "")
            sentences = output.get("sentences", [])

            words: list[TranscriptionWord] = []
            for sent in sentences:
                for w in sent.get("words", []):
                    words.append(
                        TranscriptionWord(
                            text=w.get("text", ""),
                            start_ms=w.get("begin_time", 0),
                            end_ms=w.get("end_time", 0),
                        )
                    )

            return TranscriptionResult(
                text=text,
                words=words,
                language=language or "",
                confidence=1.0,
            )
        except Exception as e:
            return TranscriptionResult(text="", metadata={"error": str(e)})


# ---------------------------------------------------------------------------
# Streaming STT (chunked audio → incremental transcripts)
# ---------------------------------------------------------------------------


async def speech_to_text_streaming(
    audio_chunks: list[bytes],
    provider: str = "openai",
    *,
    model: str | None = None,
    language: str | None = None,
    sample_rate: int = 16000,
    on_partial: callable | None = None,
    timeout: float = 120.0,
) -> TranscriptionResult:
    """Transcribe audio in streaming fashion — concatenate chunks and transcribe.

    For true real-time streaming STT, use WebSocket-based providers (deepgram
    streaming, assemblyai real-time). This implementation collects all chunks
    and does a single batch transcription, calling on_partial for progress.

    Args:
        audio_chunks: List of audio byte chunks in order.
        provider: STT provider.
        model: Model name.
        language: Language code.
        sample_rate: Sample rate.
        on_partial: Optional callback(partial_text: str) for progress.
        timeout: Timeout.

    Returns:
        TranscriptionResult.
    """
    full_audio = b"".join(audio_chunks)

    # Fire partial callback with interim estimate
    if on_partial and full_audio:
        dur_estimate = len(full_audio) / (sample_rate * 2)  # rough seconds
        on_partial(f"[transcribing ~{dur_estimate:.1f}s of audio...]")

    result = await speech_to_text(
        full_audio,
        provider=provider,
        model=model,
        language=language,
        sample_rate=sample_rate,
        timeout=timeout,
    )

    if on_partial and result.text:
        on_partial(result.text)

    return result


# ---------------------------------------------------------------------------
# Convenience: transcribe audio file
# ---------------------------------------------------------------------------


async def transcribe_file(
    file_path: str,
    provider: str = "openai",
    *,
    model: str | None = None,
    language: str | None = None,
) -> TranscriptionResult:
    """Transcribe an audio file on disk.

    Args:
        file_path: Path to audio file (WAV, MP3, etc. — provider-dependent).
        provider: STT provider.
        model: Model name.
        language: Language code.

    Returns:
        TranscriptionResult.
    """
    with open(file_path, "rb") as f:
        audio = f.read()

    # Detect sample rate from WAV header
    sample_rate = 16000
    if audio[:4] == b"RIFF":
        _, rate, _, _ = __import__("veya.oprim.audio", fromlist=["wav_to_pcm"]).wav_to_pcm(audio)
        sample_rate = rate

    return await speech_to_text(
        audio,
        provider=provider,
        model=model,
        language=language,
        sample_rate=sample_rate,
    )
