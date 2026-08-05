"""
veya/omodul/voice_agent.py — Voice Conversation Module (Layer 3).

End-to-end voice agent module built on oskill + oprim.
Orchestrates the full voice pipeline: audio in → VAD → STT → LLM → TTS → audio out.

Supports multi-turn conversation, interruption handling, and streaming responses.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from veya.oprim.types import (
    AudioConfig,
    AudioFrame,
    TurnDecision,
    TurnState,
    VADResult,
    VADState,
)
from veya.oprim.vad import build_vad_segments, vad_energy
from veya.oskill.audio_io import AudioPipeline, MemoryAudioSink, MemoryAudioSource
from veya.oskill.stt import speech_to_text
from veya.oskill.tts import text_to_speech
from veya.oskill.turn_detection import (
    EndpointingConfig,
    InterruptionConfig,
    TurnDetector,
    TurnHandlingConfig,
)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class VoiceAgentState(StrEnum):
    """Voice agent session states."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    DONE = "done"
    ERROR = "error"


@dataclass
class VoiceSessionConfig:
    """Configuration for a voice agent session."""

    sample_rate: int = 16000
    frame_duration_ms: int = 20
    language: str = "en"
    stt_provider: str = "openai"
    stt_model: str | None = None
    tts_provider: str = "openai"
    tts_model: str | None = None
    tts_voice: str | None = None
    llm_provider: str = "openai"
    llm_model: str | None = None
    max_turns: int = 20
    max_turn_duration_ms: float = 30000
    greeting_enabled: bool = True


@dataclass
class VoiceSessionResult:
    """Result of a voice agent session."""

    transcript: list[dict] = field(default_factory=list)
    audio_output: bytes = b""
    state: VoiceAgentState = VoiceAgentState.IDLE
    turns: int = 0
    total_duration_ms: float = 0.0
    error: str = ""


# ---------------------------------------------------------------------------
# Voice Agent — orchestrates the full voice pipeline
# ---------------------------------------------------------------------------


class VoiceAgent:
    """End-to-end voice conversation agent.

    Manages the complete voice pipeline:
    1. Capture audio from user (mic/file/memory)
    2. Detect speech using VAD
    3. Transcribe speech using STT
    4. Generate response using LLM
    5. Synthesize speech using TTS
    6. Play audio to user

    Example:
        >>> agent = VoiceAgent(VoiceSessionConfig())
        >>> agent.llm_handler = my_llm_handler
        >>> result = await agent.run_conversation(audio_bytes)
        >>> print(result.transcript)
    """

    def __init__(self, config: VoiceSessionConfig | None = None):
        self.config = config or VoiceSessionConfig()
        self.state = VoiceAgentState.IDLE
        self._turn_detector = TurnDetector(
            TurnHandlingConfig(
                endpointing=EndpointingConfig(min_delay_ms=500, max_delay_ms=3000),
                interruption=InterruptionConfig(enabled=True),
            ),
            sample_rate=self.config.sample_rate,
        )
        self._pipeline: AudioPipeline | None = None

        # Handlers (injected by caller)
        self.llm_handler: Callable[[str, list[dict]], Any] | None = None
        self.on_state_change: Callable[[VoiceAgentState, dict], None] | None = None
        self.on_transcript: Callable[[str, bool], None] | None = None

    def _set_state(self, state: VoiceAgentState, extra: dict | None = None):
        self.state = state
        if self.on_state_change:
            self.on_state_change(state, extra or {})

    async def run_conversation(
        self,
        audio_input: bytes,
        *,
        system_prompt: str = "You are a helpful voice assistant.",
        initial_greeting: str | None = None,
        conversation_history: list[dict] | None = None,
    ) -> VoiceSessionResult:
        """Run a complete voice conversation on pre-recorded audio.

        For real-time streaming, use run_streaming() instead.

        Args:
            audio_input: Complete audio bytes (PCM 16-bit).
            system_prompt: System prompt for the LLM.
            initial_greeting: Optional greeting to say first.
            conversation_history: Previous conversation turns.

        Returns:
            VoiceSessionResult with transcript and audio output.
        """
        result = VoiceSessionResult()
        history = conversation_history or []
        start_time = time.time()

        # Optional greeting
        output_audio = b""
        if initial_greeting and self.config.greeting_enabled:
            self._set_state(VoiceAgentState.SPEAKING, {"text": initial_greeting})
            try:
                greeting_audio = await text_to_speech(
                    initial_greeting,
                    provider=self.config.tts_provider,
                    model=self.config.tts_model,
                    voice=self.config.tts_voice,
                )
                output_audio += greeting_audio
            except Exception as e:
                result.error = f"TTS greeting failed: {e}"

        # Process audio in frame-sized chunks
        from veya.oprim.audio import split_into_frames
        frames_data = split_into_frames(
            audio_input,
            frame_duration_ms=self.config.frame_duration_ms,
            sample_rate=self.config.sample_rate,
        )

        accumulated_speech: list[bytes] = []
        user_texts: list[str] = []

        for i, frame_data in enumerate(frames_data):
            frame = AudioFrame(
                data=frame_data,
                sample_rate=self.config.sample_rate,
                timestamp_ms=i * self.config.frame_duration_ms,
            )
            vad_result = vad_energy(frame)
            decision = self._turn_detector.process_frame(frame, vad_result)

            # Collect speech frames
            if vad_result.is_speech:
                accumulated_speech.append(frame_data)

            # Endpoint detected — process turn
            if decision.state in (TurnState.USER_DONE, TurnState.INTERRUPTION):
                if accumulated_speech:
                    # STT: transcribe accumulated speech
                    self._set_state(VoiceAgentState.THINKING)
                    speech_bytes = b"".join(accumulated_speech)
                    transcript = await speech_to_text(
                        speech_bytes,
                        provider=self.config.stt_provider,
                        model=self.config.stt_model,
                        language=self.config.language,
                        sample_rate=self.config.sample_rate,
                    )

                    if transcript.text.strip():
                        user_texts.append(transcript.text)
                        result.transcript.append({
                            "role": "user",
                            "text": transcript.text,
                            "timestamp_ms": frame.timestamp_ms,
                        })
                        if self.on_transcript:
                            self.on_transcript(transcript.text, True)

                        # LLM: generate response
                        self._set_state(VoiceAgentState.THINKING)
                        if self.llm_handler:
                            try:
                                llm_response = await self._call_llm(
                                    transcript.text, history, system_prompt
                                )
                                agent_text = llm_response.get("content", "")
                            except Exception as e:
                                agent_text = f"[LLM error: {e}]"
                        else:
                            agent_text = f"I heard: {transcript.text}"

                        result.transcript.append({
                            "role": "assistant",
                            "text": agent_text,
                            "timestamp_ms": frame.timestamp_ms,
                        })

                        # TTS: synthesize response
                        self._set_state(VoiceAgentState.SPEAKING, {"text": agent_text})
                        try:
                            agent_audio = await text_to_speech(
                                agent_text,
                                provider=self.config.tts_provider,
                                model=self.config.tts_model,
                                voice=self.config.tts_voice,
                            )
                            output_audio += agent_audio
                        except Exception as e:
                            result.error = f"TTS failed: {e}"

                        # Update history
                        history.append({"role": "user", "content": transcript.text})
                        history.append({"role": "assistant", "content": agent_text})

                        result.turns += 1
                        accumulated_speech = []

                    # Check turn limit
                    if result.turns >= self.config.max_turns:
                        break

                self._turn_detector.agent_stopped_speaking()

            # Agent speaking — check for interruption
            elif decision.state == TurnState.INTERRUPTION:
                self._set_state(VoiceAgentState.INTERRUPTED)
                accumulated_speech = [frame_data]

        # Final cleanup
        result.audio_output = output_audio
        result.state = VoiceAgentState.DONE if not result.error else VoiceAgentState.ERROR
        result.total_duration_ms = (time.time() - start_time) * 1000

        self._set_state(result.state)
        return result

    async def _call_llm(
        self,
        user_text: str,
        history: list[dict],
        system_prompt: str,
    ) -> dict:
        """Call the LLM handler with conversation context."""
        if self.llm_handler is None:
            return {"content": f"Echo: {user_text}"}

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history[-10:])  # last 10 turns
        messages.append({"role": "user", "content": user_text})

        if asyncio.iscoroutinefunction(self.llm_handler):
            return await self.llm_handler(messages)
        else:
            return self.llm_handler(messages)

    async def run_streaming(
        self,
        audio_source: AsyncIterator[bytes],
        *,
        system_prompt: str = "You are a helpful voice assistant.",
        on_audio_chunk: Callable[[bytes], None] | None = None,
    ) -> VoiceSessionResult:
        """Run a streaming voice conversation with real-time audio I/O.

        Args:
            audio_source: Async iterator yielding audio chunks.
            system_prompt: System prompt for LLM.
            on_audio_chunk: Callback for TTS audio chunks as they're generated.

        Returns:
            VoiceSessionResult.
        """
        result = VoiceSessionResult()
        history: list[dict] = []
        accumulated_speech: list[bytes] = []

        self._set_state(VoiceAgentState.LISTENING)

        async for chunk in audio_source:
            frames = split_into_frames(
                chunk,
                frame_duration_ms=self.config.frame_duration_ms,
                sample_rate=self.config.sample_rate,
            )

            for i, frame_data in enumerate(frames):
                frame = AudioFrame(
                    data=frame_data,
                    sample_rate=self.config.sample_rate,
                    timestamp_ms=i * self.config.frame_duration_ms,
                )
                vad_result = vad_energy(frame)
                decision = self._turn_detector.process_frame(frame, vad_result)

                if vad_result.is_speech:
                    accumulated_speech.append(frame_data)

                if decision.state == TurnState.USER_DONE and accumulated_speech:
                    # Process turn (same as run_conversation)
                    speech_bytes = b"".join(accumulated_speech)
                    transcript = await speech_to_text(
                        speech_bytes,
                        provider=self.config.stt_provider,
                        language=self.config.language,
                        sample_rate=self.config.sample_rate,
                    )

                    if transcript.text.strip():
                        result.transcript.append({"role": "user", "text": transcript.text})
                        if self.on_transcript:
                            self.on_transcript(transcript.text, True)

                        self._set_state(VoiceAgentState.THINKING)
                        agent_text = "I understand."
                        if self.llm_handler:
                            try:
                                resp = await self._call_llm(transcript.text, history, system_prompt)
                                agent_text = resp.get("content", agent_text)
                            except Exception:
                                pass

                        result.transcript.append({"role": "assistant", "text": agent_text})
                        self._set_state(VoiceAgentState.SPEAKING, {"text": agent_text})

                        # Stream TTS output
                        from veya.oskill.tts import text_to_speech_streaming
                        async for audio_chunk in text_to_speech_streaming(
                            agent_text,
                            provider=self.config.tts_provider,
                            voice=self.config.tts_voice,
                        ):
                            if on_audio_chunk:
                                on_audio_chunk(audio_chunk)
                            result.audio_output += audio_chunk

                        history.append({"role": "user", "content": transcript.text})
                        history.append({"role": "assistant", "content": agent_text})
                        result.turns += 1
                        accumulated_speech = []

                    self._set_state(VoiceAgentState.LISTENING)

                if result.turns >= self.config.max_turns:
                    break

        result.state = VoiceAgentState.DONE
        self._set_state(result.state)
        return result


# ---------------------------------------------------------------------------
# omodul interface — matches the 3O omodul contract
# ---------------------------------------------------------------------------


async def run_voice_conversation(
    config: Any,
    input_data: Any,
    output_dir: Path = Path("/tmp/veya"),
) -> dict[str, Any]:
    """omodul contract: run a voice conversation from config + input.

    Args:
        config: SimpleNamespace or dict with voice session configuration.
        input_data: SimpleNamespace or dict with audio_input, system_prompt, etc.
        output_dir: Output directory for artifacts.

    Returns:
        Dict with result, transcript, audio_output_path, stats.
    """
    from types import SimpleNamespace

    if isinstance(input_data, dict):
        input_data = SimpleNamespace(**input_data)
    if isinstance(config, dict):
        config = SimpleNamespace(**config)

    audio_input = getattr(input_data, "audio_input", b"")
    system_prompt = getattr(input_data, "system_prompt", "You are a helpful voice assistant.")
    initial_greeting = getattr(input_data, "initial_greeting", None)
    history = getattr(input_data, "conversation_history", None)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    session_config = VoiceSessionConfig(
        sample_rate=getattr(config, "sample_rate", 16000),
        language=getattr(config, "language", "en"),
        stt_provider=getattr(config, "stt_provider", "openai"),
        stt_model=getattr(config, "stt_model", None),
        tts_provider=getattr(config, "tts_provider", "openai"),
        tts_model=getattr(config, "tts_model", None),
        tts_voice=getattr(config, "tts_voice", None),
        max_turns=getattr(config, "max_turns", 20),
    )

    agent = VoiceAgent(session_config)

    # Wire LLM handler if provided
    llm_handler = getattr(input_data, "llm_handler", None)
    if llm_handler:
        agent.llm_handler = llm_handler

    result = await agent.run_conversation(
        audio_input,
        system_prompt=system_prompt,
        initial_greeting=initial_greeting,
        conversation_history=history,
    )

    # Save output audio
    audio_path = output_dir / "output_audio.wav"
    if result.audio_output:
        from veya.oprim.audio import pcm_to_wav
        wav_data = pcm_to_wav(
            result.audio_output,
            sample_rate=session_config.sample_rate,
        )
        audio_path.write_bytes(wav_data)

    return {
        "status": "completed" if result.state == VoiceAgentState.DONE else "error",
        "transcript": result.transcript,
        "audio_output_path": str(audio_path),
        "turns": result.turns,
        "duration_ms": result.total_duration_ms,
        "error": result.error,
        "state": result.state.value,
    }
