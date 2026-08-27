"""
veya/oskill/audio_io.py — Audio I/O Pipeline Management (Layer 2).

Composite skill for managing audio input/output devices and streaming.
Built on oprim audio ops + OS-level audio APIs.
Provides async audio capture and playback with configurable pipelines.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from veya.oprim.audio import (
    concat_audio_frames,
    pcm_to_wav,
    split_into_frames,
)
from veya.oprim.types import AudioConfig, AudioFrame


class AudioDeviceType(StrEnum):
    """Audio device types."""

    MICROPHONE = "microphone"
    SPEAKER = "speaker"
    FILE = "file"
    MEMORY = "memory"
    NETWORK = "network"


@dataclass
class AudioDevice:
    """Audio device descriptor."""

    name: str
    device_type: AudioDeviceType
    sample_rate: int = 16000
    channels: int = 1
    is_default: bool = False


# ---------------------------------------------------------------------------
# Audio Pipeline — manages input → processing → output
# ---------------------------------------------------------------------------


class AudioPipeline:
    """Manages an audio I/O pipeline with async streaming.

    Handles capturing audio from a source (mic/file/memory), applying
    optional processing, and delivering to a sink (speaker/file/callback).

    Example:
        >>> pipeline = AudioPipeline(config=AudioConfig(sample_rate=16000))
        >>> pipeline.set_input_callback(my_audio_generator)
        >>> pipeline.set_output_callback(my_audio_player)
        >>> await pipeline.start()
        >>> # Audio flows: generator → pipeline → player
        >>> await pipeline.stop()
    """

    def __init__(self, config: AudioConfig | None = None):
        self.config = config or AudioConfig()
        self._running = False
        self._input_callback: Callable | None = None
        self._output_callback: Callable[[bytes], None] | None = None
        self._frame_queue: asyncio.Queue[AudioFrame] = asyncio.Queue(maxsize=512)
        self._tasks: list[asyncio.Task] = []
        self._total_frames = 0
        self._total_bytes = 0

    def set_input_callback(self, callback: Callable[[], AsyncIterator[bytes]]):
        """Set an async generator that produces audio bytes."""
        self._input_callback = callback

    def set_output_callback(self, callback: Callable[[bytes], None]):
        """Set a callback that receives audio bytes for playback."""
        self._output_callback = callback

    async def push_frame(self, frame: AudioFrame):
        """Push an audio frame into the pipeline."""
        if not self._running:
            return
        await self._frame_queue.put(frame)

    async def start(self):
        """Start the audio pipeline."""
        self._running = True

        if self._input_callback:
            self._tasks.append(asyncio.create_task(self._run_input_loop(), name="audio_input"))

        if self._output_callback:
            self._tasks.append(asyncio.create_task(self._run_output_loop(), name="audio_output"))

    async def stop(self):
        """Stop the audio pipeline."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run_input_loop(self):
        """Read audio from input callback and push frames to queue."""
        if self._input_callback is None:
            return
        try:
            async for chunk in self._input_callback():
                if not self._running:
                    break
                frames = split_into_frames(
                    chunk,
                    frame_duration_ms=self.config.frame_duration_ms,
                    sample_rate=self.config.sample_rate,
                    channels=self.config.channels,
                )
                for _i, frame_data in enumerate(frames):
                    frame = AudioFrame(
                        data=frame_data,
                        sample_rate=self.config.sample_rate,
                        channels=self.config.channels,
                        timestamp_ms=self._total_frames * self.config.frame_duration_ms,
                    )
                    await self._frame_queue.put(frame)
                    self._total_frames += 1
                    self._total_bytes += len(frame_data)
        except asyncio.CancelledError:
            pass

    async def _run_output_loop(self):
        """Read frames from queue and deliver to output callback."""
        if self._output_callback is None:
            return
        try:
            while self._running:
                try:
                    frame = await asyncio.wait_for(self._frame_queue.get(), timeout=1.0)
                    self._output_callback(frame.data)
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass

    async def read_frame(self, timeout: float = 5.0) -> AudioFrame | None:
        """Read a single audio frame from the pipeline."""
        try:
            return await asyncio.wait_for(self._frame_queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def read_all(self) -> bytes:
        """Read all available audio frames and concatenate."""
        chunks: list[bytes] = []
        while not self._frame_queue.empty():
            frame = self._frame_queue.get_nowait()
            chunks.append(frame.data)
        return concat_audio_frames(chunks) if chunks else b""

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_frames": self._total_frames,
            "total_bytes": self._total_bytes,
            "duration_ms": self._total_frames * self.config.frame_duration_ms,
            "running": self._running,
        }


# ---------------------------------------------------------------------------
# Memory-based audio source/sink (for testing & offline use)
# ---------------------------------------------------------------------------


class MemoryAudioSource:
    """In-memory audio source — feeds pre-recorded audio through the pipeline."""

    def __init__(self, audio_data: bytes, chunk_size_ms: int = 100):
        self.audio_data = audio_data
        self.chunk_size_ms = chunk_size_ms
        self._pos = 0

    async def read(self, sample_rate: int = 16000, channels: int = 1) -> AsyncIterator[bytes]:
        """Stream audio data in chunks."""
        frame_bytes = int(sample_rate * self.chunk_size_ms / 1000) * channels * 2
        while self._pos < len(self.audio_data):
            chunk = self.audio_data[self._pos : self._pos + frame_bytes]
            self._pos += frame_bytes
            if chunk:
                yield chunk
            await asyncio.sleep(self.chunk_size_ms / 1000.0)


class MemoryAudioSink:
    """In-memory audio sink — collects audio for later use."""

    def __init__(self):
        self.buffer: list[bytes] = []

    def write(self, data: bytes):
        self.buffer.append(data)

    def get_audio(self) -> bytes:
        return b"".join(self.buffer)

    def clear(self):
        self.buffer.clear()

    def save_wav(self, path: str, sample_rate: int = 16000, channels: int = 1):
        """Save collected audio as a WAV file."""
        data = self.get_audio()
        wav_data = pcm_to_wav(data, sample_rate=sample_rate, channels=channels)
        with open(path, "wb") as f:
            f.write(wav_data)


# ---------------------------------------------------------------------------
# Audio device discovery
# ---------------------------------------------------------------------------


def list_audio_devices() -> list[AudioDevice]:
    """List available audio input/output devices.

    Returns:
        List of AudioDevice descriptors. Falls back to defaults if
        audio libraries are unavailable.

    Example:
        >>> devices = list_audio_devices()
        >>> for d in devices:
        ...     print(f"{d.device_type}: {d.name}")
    """
    devices: list[AudioDevice] = []

    try:
        import sounddevice as sd

        default_input = sd.query_devices(kind="input")
        default_output = sd.query_devices(kind="output")

        for _i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                devices.append(
                    AudioDevice(
                        name=dev["name"],
                        device_type=AudioDeviceType.MICROPHONE,
                        sample_rate=int(dev["default_samplerate"]),
                        channels=dev["max_input_channels"],
                        is_default=(dev["name"] == default_input["name"]),
                    )
                )
            if dev["max_output_channels"] > 0:
                devices.append(
                    AudioDevice(
                        name=dev["name"],
                        device_type=AudioDeviceType.SPEAKER,
                        sample_rate=int(dev["default_samplerate"]),
                        channels=dev["max_output_channels"],
                        is_default=(dev["name"] == default_output["name"]),
                    )
                )
    except ImportError:
        # Fallback: virtual devices
        devices.extend(
            [
                AudioDevice(
                    name="Default Microphone",
                    device_type=AudioDeviceType.MICROPHONE,
                    is_default=True,
                ),
                AudioDevice(
                    name="Default Speaker", device_type=AudioDeviceType.SPEAKER, is_default=True
                ),
            ]
        )

    return devices


# ---------------------------------------------------------------------------
# Convenience: create pipeline
# ---------------------------------------------------------------------------


def create_audio_pipeline(
    input_device: str | None = None,
    output_device: str | None = None,
    sample_rate: int = 16000,
    channels: int = 1,
    frame_duration_ms: int = 20,
) -> AudioPipeline:
    """Create an audio pipeline with optional device binding.

    Args:
        input_device: Input device name (None = default).
        output_device: Output device name (None = default).
        sample_rate: Audio sample rate.
        channels: Number of channels.
        frame_duration_ms: Frame duration.

    Returns:
        Configured AudioPipeline.
    """
    config = AudioConfig(
        sample_rate=sample_rate,
        channels=channels,
        frame_duration_ms=frame_duration_ms,
    )
    return AudioPipeline(config=config)
