"""
veya/oprim/types.py — Core dataclasses for audio, video, and VAD operations.

Part of the 3O Layer 1 (oprim): stateless, pure-type definitions.
No I/O, no provider calls, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


# ---------------------------------------------------------------------------
# Audio types
# ---------------------------------------------------------------------------


class AudioFormat(StrEnum):
    """Supported audio container/codec formats."""

    PCM_S16LE = "pcm_s16le"  # 16-bit signed little-endian PCM
    PCM_F32LE = "pcm_f32le"  # 32-bit float little-endian PCM
    WAV = "wav"
    MP3 = "mp3"
    OPUS = "opus"
    OGG = "ogg"
    FLAC = "flac"
    AAC = "aac"
    WEBM_AUDIO = "webm"


@dataclass(frozen=True)
class AudioConfig:
    """Immutable audio stream configuration."""

    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2  # bytes per sample (2 = 16-bit)
    frame_duration_ms: int = 20  # ms per audio frame
    format: AudioFormat = AudioFormat.PCM_S16LE

    @property
    def frame_size(self) -> int:
        """Number of bytes per frame."""
        samples_per_frame = int(self.sample_rate * self.frame_duration_ms / 1000)
        return samples_per_frame * self.channels * self.sample_width

    @property
    def frames_per_second(self) -> float:
        """Number of audio frames per second."""
        return 1000.0 / self.frame_duration_ms


@dataclass(frozen=True)
class AudioFrame:
    """A single audio frame (e.g., 20ms of PCM data)."""

    data: bytes
    sample_rate: int = 16000
    channels: int = 1
    timestamp_ms: float = 0.0  # relative to stream start

    @property
    def duration_ms(self) -> float:
        """Duration of this frame in milliseconds."""
        bytes_per_sample = 2  # assuming 16-bit
        total_samples = len(self.data) / (self.channels * bytes_per_sample)
        return (total_samples / self.sample_rate) * 1000

    @property
    def is_silence(self) -> bool:
        """Quick check: is this frame all zero bytes?"""
        return not any(self.data)


# ---------------------------------------------------------------------------
# VAD types
# ---------------------------------------------------------------------------


class VADState(StrEnum):
    """Voice activity state."""

    SILENCE = "silence"
    SPEECH = "speech"
    STARTING = "starting"  # transition from silence to speech
    ENDING = "ending"  # transition from speech to silence


@dataclass(frozen=True)
class VADResult:
    """Result of a single VAD frame analysis."""

    state: VADState
    confidence: float  # 0.0 = certain silence, 1.0 = certain speech
    energy_db: float  # RMS energy in dB
    frame: AudioFrame | None = None  # the analyzed frame (if available)

    @property
    def is_speech(self) -> bool:
        return self.state in (VADState.SPEECH, VADState.STARTING)


@dataclass
class VADSegment:
    """A detected speech segment from VAD analysis."""

    start_ms: float
    end_ms: float
    frames: list[VADResult] = field(default_factory=list)
    audio: bytes = b""  # concatenated audio data for this segment

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms


# ---------------------------------------------------------------------------
# Video / Image types
# ---------------------------------------------------------------------------


class ImageFormat(StrEnum):
    """Supported image formats."""

    PNG = "png"
    JPEG = "jpeg"
    JPG = "jpg"
    WEBP = "webp"
    BMP = "bmp"
    GIF = "gif"


@dataclass(frozen=True)
class ImageFrame:
    """A single image/frame with metadata."""

    data: bytes  # raw image bytes
    width: int = 0
    height: int = 0
    format: ImageFormat = ImageFormat.PNG
    timestamp_ms: float = 0.0

    @property
    def size_kb(self) -> float:
        return len(self.data) / 1024.0

    @property
    def mime_type(self) -> str:
        _mime_map: dict[ImageFormat, str] = {
            ImageFormat.PNG: "image/png",
            ImageFormat.JPEG: "image/jpeg",
            ImageFormat.JPG: "image/jpeg",
            ImageFormat.WEBP: "image/webp",
            ImageFormat.BMP: "image/bmp",
            ImageFormat.GIF: "image/gif",
        }
        return _mime_map.get(self.format, "image/png")


# ---------------------------------------------------------------------------
# Transcription types
# ---------------------------------------------------------------------------


@dataclass
class TranscriptionWord:
    """A single word with timing information."""

    text: str
    start_ms: float
    end_ms: float
    confidence: float = 1.0
    speaker_id: str | None = None


@dataclass
class TranscriptionResult:
    """Complete transcription result from STT."""

    text: str
    words: list[TranscriptionWord] = field(default_factory=list)
    language: str = ""
    confidence: float = 1.0
    is_final: bool = True
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def word_count(self) -> int:
        return len(self.words)


# ---------------------------------------------------------------------------
# Turn detection types
# ---------------------------------------------------------------------------


class TurnState(StrEnum):
    """Turn detection states."""

    USER_SPEAKING = "user_speaking"
    USER_DONE = "user_done"  # endpoint detected
    AGENT_SPEAKING = "agent_speaking"
    INTERRUPTION = "interruption"  # user interrupted agent
    IDLE = "idle"


@dataclass
class TurnDecision:
    """Decision from turn detection analysis."""

    state: TurnState
    confidence: float = 1.0
    reason: str = ""
    speech_segment: VADSegment | None = None
    transcript: TranscriptionResult | None = None


# ---------------------------------------------------------------------------
# Vision types
# ---------------------------------------------------------------------------


@dataclass
class VisionResult:
    """Result from vision analysis pipeline."""

    description: str
    objects: list[str] = field(default_factory=list)
    text_in_image: str = ""
    confidence: float = 1.0
    model: str = ""
    processing_time_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VideoFrameSample:
    """A sampled frame from a video stream."""

    image: ImageFrame
    timestamp_ms: float
    is_keyframe: bool = False
