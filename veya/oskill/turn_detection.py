"""
veya/oskill/turn_detection.py — Turn Detection & Endpointing (Layer 2).

Composite skill built on oprim VAD + timing state machine.
Determines when a user has finished speaking (endpointing) and manages
interruption detection for barge-in scenarios.

Inspired by LiveKit Agents' TurnHandlingOptions + EndpointingOptions.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from veya.oprim.types import (
    AudioFrame,
    TurnDecision,
    TurnState,
    VADResult,
    VADSegment,
    VADState,
)
from veya.oprim.vad import build_vad_segments, vad_energy, vad_frame


# ---------------------------------------------------------------------------
# Turn detection configuration
# ---------------------------------------------------------------------------


@dataclass
class EndpointingConfig:
    """Configuration for endpointing (when the user is done speaking)."""

    mode: Literal["vad", "semantic", "hybrid"] = "vad"
    min_delay_ms: float = 500   # minimum silence before endpoint
    max_delay_ms: float = 3000  # maximum silence before forcing endpoint
    alpha: float = 1.0          # sensitivity multiplier (higher = faster endpoint)


@dataclass
class InterruptionConfig:
    """Configuration for interruption (user barge-in during agent speech)."""

    enabled: bool = True
    min_duration_ms: float = 300    # minimum speech duration to count as interruption
    min_words: int = 1              # minimum words for interruption
    resume_false_interruption: bool = True  # agent resumes after false interruption


@dataclass
class TurnHandlingConfig:
    """Full turn handling configuration."""

    endpointing: EndpointingConfig = field(default_factory=EndpointingConfig)
    interruption: InterruptionConfig = field(default_factory=InterruptionConfig)
    user_turn_limit_ms: float = 30000  # max user turn duration


# ---------------------------------------------------------------------------
# Turn Detection State Machine
# ---------------------------------------------------------------------------


class TurnDetector:
    """Stateful turn detector — manages speech turn lifecycle.

    Tracks transitions between user_speaking → user_done → agent_speaking
    with endpointing and interruption support.

    Example:
        >>> detector = TurnDetector()
        >>> for frame in audio_frames:
        ...     decision = detector.process_frame(frame, vad_result)
        ...     if decision.state == TurnState.USER_DONE:
        ...         break  # user finished speaking
    """

    def __init__(
        self,
        config: TurnHandlingConfig | None = None,
        *,
        sample_rate: int = 16000,
    ):
        self.config = config or TurnHandlingConfig()
        self.sample_rate = sample_rate

        # Runtime state
        self._state: TurnState = TurnState.IDLE
        self._vad_frames: list[tuple[AudioFrame, VADResult]] = []
        self._speech_start_ms: float = 0.0
        self._silence_start_ms: float = 0.0
        self._last_speech_ms: float = 0.0
        self._vad_state: dict = {}  # internal VAD hangover state

    @property
    def state(self) -> TurnState:
        return self._state

    def process_frame(self, frame: AudioFrame, vad: VADResult) -> TurnDecision:
        """Process a single audio frame through the turn state machine.

        Args:
            frame: Audio frame.
            vad: VAD result for this frame.

        Returns:
            TurnDecision with current state and transition info.
        """
        self._vad_frames.append((frame, vad))

        current_time_ms = frame.timestamp_ms
        ep_config = self.config.endpointing
        int_config = self.config.interruption

        if self._state == TurnState.IDLE:
            if vad.is_speech:
                self._state = TurnState.USER_SPEAKING
                self._speech_start_ms = current_time_ms
                self._last_speech_ms = current_time_ms
                return TurnDecision(
                    state=TurnState.USER_SPEAKING,
                    reason="speech_detected",
                )
            return TurnDecision(state=TurnState.IDLE, reason="silence")

        elif self._state == TurnState.USER_SPEAKING:
            if vad.is_speech:
                self._last_speech_ms = current_time_ms
                self._silence_start_ms = 0.0
            else:
                if self._silence_start_ms == 0.0:
                    self._silence_start_ms = current_time_ms

                silence_dur = current_time_ms - self._silence_start_ms
                effective_delay = ep_config.min_delay_ms / ep_config.alpha

                if silence_dur >= effective_delay:
                    segment = self._build_segment()
                    self._state = TurnState.IDLE
                    return TurnDecision(
                        state=TurnState.USER_DONE,
                        confidence=min(1.0, silence_dur / ep_config.max_delay_ms),
                        reason=f"endpoint: {silence_dur:.0f}ms silence",
                        speech_segment=segment,
                    )

            # User turn timeout
            turn_dur = current_time_ms - self._speech_start_ms
            if turn_dur >= self.config.user_turn_limit_ms:
                segment = self._build_segment()
                self._state = TurnState.IDLE
                return TurnDecision(
                    state=TurnState.USER_DONE,
                    reason=f"turn_limit: {turn_dur:.0f}ms",
                    speech_segment=segment,
                )

            return TurnDecision(state=TurnState.USER_SPEAKING, reason="speaking")

        elif self._state == TurnState.AGENT_SPEAKING:
            if int_config.enabled and vad.is_speech:
                # Check if this qualifies as an interruption
                speech_dur = current_time_ms - self._speech_start_ms
                if speech_dur >= int_config.min_duration_ms:
                    self._state = TurnState.INTERRUPTION
                    return TurnDecision(
                        state=TurnState.INTERRUPTION,
                        reason=f"interruption after {speech_dur:.0f}ms",
                    )

            return TurnDecision(state=TurnState.AGENT_SPEAKING, reason="agent_speaking")

        elif self._state == TurnState.INTERRUPTION:
            # After interruption, treat as new user turn
            if vad.is_speech:
                self._last_speech_ms = current_time_ms
                return TurnDecision(state=TurnState.INTERRUPTION, reason="interrupting")
            else:
                # Silence after interruption — user done
                self._state = TurnState.USER_DONE
                return TurnDecision(state=TurnState.USER_DONE, reason="post_interruption")

        return TurnDecision(state=self._state)

    def agent_started_speaking(self):
        """Call when the agent begins speaking."""
        self._state = TurnState.AGENT_SPEAKING
        self._speech_start_ms = 0.0
        self._silence_start_ms = 0.0

    def agent_stopped_speaking(self):
        """Call when the agent finishes speaking."""
        if self._state == TurnState.AGENT_SPEAKING:
            self._state = TurnState.IDLE
            self._clear()

    def _build_segment(self) -> VADSegment:
        """Build a VAD segment from captured frames."""
        return build_vad_segments(self._vad_frames)[0] if self._vad_frames else (
            VADSegment(start_ms=0, end_ms=0)
        )

    def _clear(self):
        """Reset frame buffer."""
        self._vad_frames.clear()


# ---------------------------------------------------------------------------
# Convenience: endpoint detection on audio buffer
# ---------------------------------------------------------------------------


def detect_turn_end(
    audio_buffer: bytes,
    sample_rate: int = 16000,
    frame_duration_ms: int = 20,
    *,
    min_silence_ms: float = 500,
    max_silence_ms: float = 3000,
    threshold_db: float = -40.0,
) -> TurnDecision:
    """Detect when a user turn ends in a complete audio buffer.

    Simple stateless analysis: slides a VAD window over the audio and returns
    the first endpoint or the decision at end of buffer.

    Args:
        audio_buffer: Complete audio bytes (PCM).
        sample_rate: Audio sample rate.
        frame_duration_ms: Frame size for analysis.
        min_silence_ms: Minimum silence to trigger endpoint.
        max_silence_ms: Maximum silence before forced endpoint.
        threshold_db: Energy threshold for speech/silence.

    Returns:
        TurnDecision.
    """
    from veya.oprim.audio import split_into_frames

    frames = split_into_frames(
        audio_buffer,
        frame_duration_ms=frame_duration_ms,
        sample_rate=sample_rate,
    )

    detector = TurnDetector(
        TurnHandlingConfig(
            endpointing=EndpointingConfig(
                min_delay_ms=min_silence_ms,
                max_delay_ms=max_silence_ms,
            ),
        ),
        sample_rate=sample_rate,
    )

    last_decision = TurnDecision(state=TurnState.IDLE)

    for i, frame_data in enumerate(frames):
        frame = AudioFrame(
            data=frame_data,
            sample_rate=sample_rate,
            timestamp_ms=i * frame_duration_ms,
        )
        result = vad_energy(frame, speech_threshold_db=threshold_db)
        decision = detector.process_frame(frame, result)
        last_decision = decision

        if decision.state in (TurnState.USER_DONE, TurnState.INTERRUPTION):
            return decision

    return last_decision
