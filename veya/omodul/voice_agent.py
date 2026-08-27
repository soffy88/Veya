"""
veya/omodul/voice_agent.py — Voice Conversation Module (Layer 3).

End-to-end voice agent module built on oskill + oprim.
Orchestrates the full voice pipeline: audio in → VAD → STT → LLM → TTS → audio out.

Supports multi-turn conversation, interruption handling, and streaming responses.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from veya.oprim.audio import split_into_frames
from veya.oprim.types import (
    AudioFrame,
    TurnState,
)
from veya.oprim.vad import vad_energy
from veya.oskill.audio_io import AudioPipeline
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
    # OpenAI TTS 的 pcm 格式固定输出 24kHz — 不是由 API 动态返回, 这里显式声明成
    # 配置值 (而不是前端硬编码猜), 换 provider 时改这里, playback 端读这个值。
    tts_sample_rate: int = 24000
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
                        result.transcript.append(
                            {
                                "role": "user",
                                "text": transcript.text,
                                "timestamp_ms": frame.timestamp_ms,
                            }
                        )
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

                        result.transcript.append(
                            {
                                "role": "assistant",
                                "text": agent_text,
                                "timestamp_ms": frame.timestamp_ms,
                            }
                        )

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
        on_audio_chunk: Callable[[bytes], Any] | None = None,
    ) -> VoiceSessionResult:
        """Run a streaming voice conversation with real-time audio I/O.

        Unlike a naive listen→think→speak→listen loop, audio ingestion here
        never blocks on a turn's STT→LLM→TTS work — that runs as a background
        task (`turn_task`) while this loop keeps feeding frames through
        VAD/turn-detection. That's what makes real interruption (barge-in)
        possible: the turn detector is put into AGENT_SPEAKING as soon as a
        turn starts processing (not just once TTS begins — "thinking" counts
        as the agent's floor too), so if the user starts talking again before
        or during the reply, `TurnState.INTERRUPTION` fires, the in-flight
        turn_task is cancelled, and on_audio_chunk simply stops receiving
        further data (the caller — e.g. a websocket route — is expected to
        treat a VoiceAgentState.INTERRUPTED state event as "stop playback").

        Args:
            audio_source: Async iterator yielding audio chunks.
            system_prompt: System prompt for LLM.
            on_audio_chunk: Callback for TTS audio chunks as they're generated.
                May be sync or async (checked per-call, same convention as
                llm_handler in _call_llm — real transports like
                websocket.send_bytes are coroutines).

        Returns:
            VoiceSessionResult.
        """
        result = VoiceSessionResult()
        history: list[dict] = []
        accumulated_speech: list[bytes] = []
        frame_index = 0  # 全局递增 — 不能像旧版那样每个 chunk 重新从 0 数, 否则
        # TurnDetector 的静音/打断计时 (拿 frame.timestamp_ms 算) 全部失真。
        turn_task: asyncio.Task | None = None

        async def emit_audio_chunk(chunk: bytes) -> None:
            if on_audio_chunk is None:
                return
            if asyncio.iscoroutinefunction(on_audio_chunk):
                await on_audio_chunk(chunk)
            else:
                maybe_awaitable = on_audio_chunk(chunk)
                if asyncio.iscoroutine(maybe_awaitable):
                    await maybe_awaitable

        async def process_turn(speech_bytes: bytes) -> None:
            try:
                transcript = await speech_to_text(
                    speech_bytes,
                    provider=self.config.stt_provider,
                    language=self.config.language,
                    sample_rate=self.config.sample_rate,
                )
                if not transcript.text.strip():
                    return

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

                from veya.oskill.tts import text_to_speech_streaming

                async for audio_chunk in text_to_speech_streaming(
                    agent_text,
                    provider=self.config.tts_provider,
                    voice=self.config.tts_voice,
                ):
                    await emit_audio_chunk(audio_chunk)
                    result.audio_output += audio_chunk

                history.append({"role": "user", "content": transcript.text})
                history.append({"role": "assistant", "content": agent_text})
                result.turns += 1
            finally:
                # 无论正常说完/被打断取消/异常, 都要把话筒交还给用户 —
                # 否则 TurnDetector 会卡在 AGENT_SPEAKING, 后续帧全部误判。
                self._turn_detector.agent_stopped_speaking()
                self._set_state(VoiceAgentState.LISTENING)

        self._set_state(VoiceAgentState.LISTENING)

        async for chunk in audio_source:
            frames = split_into_frames(
                chunk,
                frame_duration_ms=self.config.frame_duration_ms,
                sample_rate=self.config.sample_rate,
            )

            for frame_data in frames:
                frame = AudioFrame(
                    data=frame_data,
                    sample_rate=self.config.sample_rate,
                    timestamp_ms=frame_index * self.config.frame_duration_ms,
                )
                frame_index += 1
                vad_result = vad_energy(frame)
                decision = self._turn_detector.process_frame(frame, vad_result)

                if turn_task is not None and turn_task.done():
                    turn_task = None  # 上一轮自然说完 (finally 已归还话筒)

                if decision.state == TurnState.INTERRUPTION:
                    if turn_task is not None:
                        turn_task.cancel()
                        turn_task = None
                        self._set_state(VoiceAgentState.INTERRUPTED)
                    accumulated_speech = [frame_data]
                    continue

                if turn_task is None:
                    if vad_result.is_speech:
                        accumulated_speech.append(frame_data)

                    if decision.state == TurnState.USER_DONE and accumulated_speech:
                        speech_bytes = b"".join(accumulated_speech)
                        accumulated_speech = []
                        # 从现在起 (转文字+思考+合成语音全程), 用户开口 = 打断,
                        # 不是只有真正开始放 TTS 音频那一刻才算。
                        self._turn_detector.agent_started_speaking()
                        turn_task = asyncio.create_task(process_turn(speech_bytes))

            if result.turns >= self.config.max_turns:
                break

        if turn_task is not None:
            await turn_task

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
