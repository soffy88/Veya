"""
veya/omodul/multimodal_agent.py — Multi-modal Agent Module (Layer 3).

Combined voice + vision agent that can see, hear, and speak simultaneously.
Orchestrates voice pipeline and vision pipeline in a single session.

Supports: voice conversation with visual context, real-time camera analysis,
screen sharing understanding, and document+voice interactions.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from veya.omodul.vision_agent import (
    VisionAgent,
    VisionSessionConfig,
)
from veya.omodul.voice_agent import (
    VoiceSessionConfig,
)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class MultiModalState(StrEnum):
    """Multi-modal agent states."""

    IDLE = "idle"
    LISTENING = "listening"
    SEEING = "seeing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    DONE = "done"
    ERROR = "error"


@dataclass
class MultiModalSessionConfig:
    """Configuration for a multi-modal agent session."""

    voice: VoiceSessionConfig = field(default_factory=VoiceSessionConfig)
    vision: VisionSessionConfig = field(default_factory=VisionSessionConfig)
    max_turns: int = 20


@dataclass
class MultiModalSessionResult:
    """Result of a multi-modal agent session."""

    transcript: list[dict] = field(default_factory=list)
    vision_findings: list[str] = field(default_factory=list)
    audio_output: bytes = b""
    state: MultiModalState = MultiModalState.IDLE
    turns: int = 0
    total_duration_ms: float = 0.0
    error: str = ""


# ---------------------------------------------------------------------------
# Multi-modal Agent
# ---------------------------------------------------------------------------


class MultiModalAgent:
    """Agent that combines voice and vision capabilities.

    Can process:
    - Voice input with visual context (e.g., "what's on my screen?")
    - Image-guided conversations (show me X and I'll explain it)
    - Video analysis with voice interaction

    Example:
        >>> agent = MultiModalAgent(MultiModalSessionConfig())
        >>> result = await agent.run_session(
        ...     audio_bytes, images=[screenshot_bytes],
        ...     prompt="What do you see on screen?",
        ... )
    """

    def __init__(self, config: MultiModalSessionConfig | None = None):
        self.config = config or MultiModalSessionConfig()
        self.state = MultiModalState.IDLE
        self.llm_handler: Any = None  # injected by caller
        self.on_state_change: Any = None

    def _set_state(self, state: MultiModalState, extra: dict | None = None):
        self.state = state
        if self.on_state_change:
            self.on_state_change(state, extra or {})

    async def run_session(
        self,
        audio_input: bytes,
        *,
        images: list[bytes] | None = None,
        system_prompt: str = "You are a helpful assistant that can see and hear.",
        prompt: str | None = None,
        conversation_history: list[dict] | None = None,
    ) -> MultiModalSessionResult:
        """Run a multi-modal session with audio + optional images.

        Args:
            audio_input: Audio bytes (PCM 16-bit).
            images: Optional list of image bytes for visual context.
            system_prompt: System prompt for LLM.
            prompt: Optional explicit text prompt (overrides STT).
            conversation_history: Previous conversation.

        Returns:
            MultiModalSessionResult.
        """
        result = MultiModalSessionResult()
        start_time = time.time()

        # Step 1: Analyze images if provided
        vision_context = ""
        if images:
            self._set_state(MultiModalState.SEEING)
            vision_agent = VisionAgent(self.config.vision)

            for i, img in enumerate(images):
                try:
                    vis_result = await vision_agent.analyze_single(
                        img,
                        prompt="Describe this image briefly.",
                    )
                    if vis_result.description:
                        vision_context += f"\n[Image {i + 1}]: {vis_result.description}"
                        result.vision_findings.append(vis_result.description)
                except Exception as e:
                    result.vision_findings.append(f"[Error analyzing image {i + 1}: {e}]")

        # Step 2: Transcribe audio if no explicit prompt
        if prompt:
            user_text = prompt
        else:
            self._set_state(MultiModalState.LISTENING)
            from veya.oskill.stt import speech_to_text

            transcript = await speech_to_text(
                audio_input,
                provider=self.config.voice.stt_provider,
                language=self.config.voice.language,
                sample_rate=self.config.voice.sample_rate,
            )
            user_text = transcript.text.strip() if transcript.text else ""

        if not user_text:
            user_text = "Hello"

        result.transcript.append({"role": "user", "text": user_text})

        # Step 3: Build multi-modal LLM prompt
        self._set_state(MultiModalState.THINKING)

        # Build a prompt that includes visual context
        full_prompt = user_text
        if vision_context:
            full_prompt = (
                f"Visual context (what the camera sees):\n{vision_context}\n\n"
                f"User said: {user_text}\n\n"
                f"Respond naturally as if you can see what the user sees."
            )

        # Step 4: Call LLM
        if self.llm_handler:
            history = conversation_history or []
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(history[-10:])
            messages.append({"role": "user", "content": full_prompt})

            try:
                if asyncio.iscoroutinefunction(self.llm_handler):
                    llm_resp = await self.llm_handler(messages)
                else:
                    llm_resp = self.llm_handler(messages)
                agent_text = llm_resp.get("content", "I understand.")
            except Exception as e:
                agent_text = f"[LLM error: {e}]"
        else:
            agent_text = f"I see {len(images or [])} images. You said: {user_text}"

        result.transcript.append({"role": "assistant", "text": agent_text})

        # Step 5: TTS
        self._set_state(MultiModalState.SPEAKING, {"text": agent_text})
        try:
            from veya.oskill.tts import text_to_speech

            audio = await text_to_speech(
                agent_text,
                provider=self.config.voice.tts_provider,
                model=self.config.voice.tts_model,
                voice=self.config.voice.tts_voice,
            )
            result.audio_output = audio
        except Exception as e:
            result.error = f"TTS failed: {e}"

        result.turns = 1
        result.state = MultiModalState.DONE if not result.error else MultiModalState.ERROR
        result.total_duration_ms = (time.time() - start_time) * 1000

        return result

    async def run_streaming_session(
        self,
        audio_chunks: list[bytes],
        *,
        images: list[bytes] | None = None,
        system_prompt: str = "You are a helpful assistant.",
    ) -> MultiModalSessionResult:
        """Run a multi-modal session with streaming audio chunks.

        Args:
            audio_chunks: List of audio byte chunks.
            images: Optional images.
            system_prompt: System prompt.

        Returns:
            MultiModalSessionResult.
        """
        full_audio = b"".join(audio_chunks)
        return await self.run_session(
            full_audio,
            images=images,
            system_prompt=system_prompt,
        )


# ---------------------------------------------------------------------------
# omodul interface
# ---------------------------------------------------------------------------


async def run_multimodal_session(
    config: Any,
    input_data: Any,
    output_dir: Path = Path("/tmp/veya"),
) -> dict[str, Any]:
    """omodul contract: run multi-modal session from config + input.

    Args:
        config: SimpleNamespace/dict with multi-modal configuration.
        input_data: SimpleNamespace/dict with audio, images, prompt, etc.
        output_dir: Output directory.

    Returns:
        Dict with transcript, audio_output_path, vision_findings, stats.
    """
    from types import SimpleNamespace

    if isinstance(input_data, dict):
        input_data = SimpleNamespace(**input_data)
    if isinstance(config, dict):
        config = SimpleNamespace(**config)

    audio_input = getattr(input_data, "audio_input", b"")
    images = getattr(input_data, "images", None)
    prompt = getattr(input_data, "prompt", None)
    system_prompt = getattr(
        input_data, "system_prompt", "You are a helpful assistant that can see and hear."
    )
    history = getattr(input_data, "conversation_history", None)

    session_config = MultiModalSessionConfig(
        voice=VoiceSessionConfig(
            sample_rate=getattr(config, "sample_rate", 16000),
            language=getattr(config, "language", "en"),
            stt_provider=getattr(config, "stt_provider", "openai"),
            tts_provider=getattr(config, "tts_provider", "openai"),
            tts_voice=getattr(config, "tts_voice", None),
        ),
        vision=VisionSessionConfig(
            provider=getattr(config, "vision_provider", "openai"),
            model=getattr(config, "vision_model", None),
        ),
        max_turns=getattr(config, "max_turns", 20),
    )

    agent = MultiModalAgent(session_config)
    llm_handler = getattr(input_data, "llm_handler", None)
    if llm_handler:
        agent.llm_handler = llm_handler

    result = await agent.run_session(
        audio_input,
        images=images,
        system_prompt=system_prompt,
        prompt=prompt,
        conversation_history=history,
    )

    # Save output
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if result.audio_output:
        from veya.oprim.audio import pcm_to_wav

        audio_path = output_dir / "output_audio.wav"
        wav_data = pcm_to_wav(result.audio_output, sample_rate=session_config.voice.sample_rate)
        audio_path.write_bytes(wav_data)
    else:
        audio_path = output_dir / "output_audio.wav"

    transcript_path = output_dir / "transcript.json"
    import json

    transcript_path.write_text(json.dumps(result.transcript, indent=2, ensure_ascii=False))

    return {
        "status": "completed" if result.state == MultiModalState.DONE else "error",
        "transcript": result.transcript,
        "vision_findings": result.vision_findings,
        "audio_output_path": str(audio_path),
        "transcript_path": str(transcript_path),
        "turns": result.turns,
        "duration_ms": result.total_duration_ms,
        "error": result.error,
    }
