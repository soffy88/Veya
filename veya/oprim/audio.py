"""
veya/oprim/audio.py — Atomic audio operations (Layer 1).

Stateless, pure-function operations for audio encoding, decoding, resampling,
silence detection, and format conversion. Each function is a single, independent
operation with no side effects.

Dependencies: stdlib only (struct, math, wave, io). Optional: numpy (for FFT ops).
"""

from __future__ import annotations

import io
import math
import struct
import wave
from typing import Literal


# ---------------------------------------------------------------------------
# PCM ↔ WAV conversion
# ---------------------------------------------------------------------------


def pcm_to_wav(
    data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """Wrap raw PCM bytes in a WAV container.

    Args:
        data: Raw PCM audio bytes.
        sample_rate: Samples per second (e.g., 16000).
        channels: Number of audio channels (1=mono, 2=stereo).
        sample_width: Bytes per sample (1=8-bit, 2=16-bit, 4=32-bit).

    Returns:
        Complete WAV file as bytes (44-byte header + data).

    Example:
        >>> wav = pcm_to_wav(frame_data, sample_rate=16000)
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(data)
    return buf.getvalue()


def wav_to_pcm(data: bytes) -> tuple[bytes, int, int, int]:
    """Extract raw PCM from a WAV container.

    Returns:
        Tuple of (pcm_bytes, sample_rate, channels, sample_width).

    Example:
        >>> pcm, rate, ch, sw = wav_to_pcm(wav_bytes)
    """
    buf = io.BytesIO(data)
    with wave.open(buf, "rb") as wf:
        return (
            wf.readframes(wf.getnframes()),
            wf.getframerate(),
            wf.getnchannels(),
            wf.getsampwidth(),
        )


# ---------------------------------------------------------------------------
# Audio format conversion
# ---------------------------------------------------------------------------


def bytes_to_int16(data: bytes) -> list[int]:
    """Convert raw PCM bytes to a list of 16-bit signed integers."""
    count = len(data) // 2
    return list(struct.unpack(f"<{count}h", data[: count * 2]))


def int16_to_bytes(samples: list[int]) -> bytes:
    """Convert a list of 16-bit signed integers to raw PCM bytes."""
    return struct.pack(f"<{len(samples)}h", *samples)


def float32_to_int16(samples: list[float], max_val: float = 32767.0) -> list[int]:
    """Convert float samples (-1.0 to 1.0) to 16-bit signed integers."""
    return [max(-32768, min(32767, int(s * max_val))) for s in samples]


def int16_to_float32(samples: list[int], max_val: float = 32767.0) -> list[float]:
    """Convert 16-bit signed integers to float samples (-1.0 to 1.0)."""
    return [s / max_val for s in samples]


# ---------------------------------------------------------------------------
# Resampling (simple linear interpolation)
# ---------------------------------------------------------------------------


def resample_audio(
    data: bytes,
    src_rate: int,
    dst_rate: int,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """Resample PCM audio using linear interpolation.

    For production use, consider librosa or scipy.signal.resample.
    This implementation is pure-Python and stateless.

    Args:
        data: Raw PCM audio bytes.
        src_rate: Source sample rate (Hz).
        dst_rate: Destination sample rate (Hz).
        channels: Number of audio channels.
        sample_width: Bytes per sample (2 for 16-bit).

    Returns:
        Resampled PCM audio bytes.
    """
    if src_rate == dst_rate:
        return data

    ratio = dst_rate / src_rate
    samples = bytes_to_int16(data)

    # Handle multi-channel: resample each channel independently
    if channels > 1:
        result: list[int] = []
        for ch in range(channels):
            ch_samples = samples[ch::channels]
            ch_resampled = _resample_int16_channel(ch_samples, ratio)
            result.extend(ch_resampled)
        # Re-interleave: [ch0_s0, ch1_s0, ch0_s1, ch1_s1, ...]
        interleaved: list[int] = []
        ch_len = len(result) // channels
        for i in range(ch_len):
            for ch in range(channels):
                idx = ch * ch_len + i
                interleaved.append(result[idx])
        return int16_to_bytes(interleaved)

    resampled = _resample_int16_channel(samples, ratio)
    return int16_to_bytes(resampled)


def _resample_int16_channel(samples: list[int], ratio: float) -> list[int]:
    """Resample a single channel of int16 samples."""
    if not samples:
        return []
    out_len = max(1, int(len(samples) * ratio))
    result: list[int] = []
    for i in range(out_len):
        src_idx = i / ratio
        idx_lo = int(src_idx)
        idx_hi = min(idx_lo + 1, len(samples) - 1)
        frac = src_idx - idx_lo
        val = samples[idx_lo] + (samples[idx_hi] - samples[idx_lo]) * frac
        result.append(int(val))
    return result


# ---------------------------------------------------------------------------
# Silence detection (energy-based)
# ---------------------------------------------------------------------------


def compute_rms(samples: list[int]) -> float:
    """Compute root-mean-square of 16-bit integer samples.

    Returns:
        RMS value (0 to 32767 for 16-bit audio).
    """
    if not samples:
        return 0.0
    sum_sq = sum(float(s * s) for s in samples)
    return math.sqrt(sum_sq / len(samples))


def linear_to_db(rms: float, ref: float = 32767.0) -> float:
    """Convert linear RMS to decibels relative to reference.

    Args:
        rms: Root-mean-square value.
        ref: Reference value (32767 for 16-bit full scale).

    Returns:
        dB value (0 dBFS = full scale, negative = quieter).
    """
    if rms <= 0:
        return -96.0  # effectively silence floor
    return 20.0 * math.log10(rms / ref)


def db_to_linear(db: float, ref: float = 32767.0) -> float:
    """Convert decibels back to linear RMS."""
    return ref * (10.0 ** (db / 20.0))


def is_silence_frame(
    data: bytes,
    threshold_db: float = -40.0,
    sample_width: int = 2,
) -> bool:
    """Check if an audio frame is silence based on energy threshold.

    Args:
        data: Raw PCM audio bytes (one frame).
        threshold_db: Silence threshold in dBFS (default -40 dB).
        sample_width: Bytes per sample (2 for 16-bit).

    Returns:
        True if the frame is silence (below threshold).
    """
    if sample_width == 2:
        samples = bytes_to_int16(data)
    elif sample_width == 4:
        # 32-bit float: unpack as float
        count = len(data) // 4
        float_samples = list(struct.unpack(f"<{count}f", data[: count * 4]))
        rms_val = math.sqrt(sum(f * f for f in float_samples) / max(len(float_samples), 1))
        db = 20.0 * math.log10(max(rms_val, 1e-10))
        return db < threshold_db
    else:
        # Fallback: treat as 16-bit
        samples = bytes_to_int16(data)

    rms_val = compute_rms(samples)
    db = linear_to_db(rms_val)
    return db < threshold_db


def detect_silence_segments(
    data: bytes,
    sample_rate: int = 16000,
    frame_duration_ms: int = 20,
    threshold_db: float = -40.0,
    min_silence_ms: int = 300,
    min_speech_ms: int = 200,
    channels: int = 1,
    sample_width: int = 2,
) -> list[tuple[float, float]]:
    """Detect silence segments in audio data.

    Iterates frame-by-frame and returns time ranges where audio is silent.

    Args:
        data: Raw PCM audio bytes.
        sample_rate: Sample rate in Hz.
        frame_duration_ms: Analysis frame duration in ms.
        threshold_db: Silence threshold in dBFS.
        min_silence_ms: Minimum silence duration to report.
        min_speech_ms: Minimum speech duration to break a silence segment.
        channels: Number of audio channels.
        sample_width: Bytes per sample.

    Returns:
        List of (start_ms, end_ms) tuples for each silence segment.
    """
    frame_bytes = int(sample_rate * frame_duration_ms / 1000) * channels * sample_width

    silence_ranges: list[tuple[float, float]] = []
    in_silence = False
    silence_start = 0.0
    current_time_ms = 0.0

    pos = 0
    while pos + frame_bytes <= len(data):
        frame = data[pos : pos + frame_bytes]
        silent = is_silence_frame(frame, threshold_db, sample_width)

        if silent and not in_silence:
            silence_start = current_time_ms
            in_silence = True
        elif not silent and in_silence:
            silence_dur = current_time_ms - silence_start
            if silence_dur >= min_silence_ms:
                silence_ranges.append((silence_start, current_time_ms))
            in_silence = False

        current_time_ms += frame_duration_ms
        pos += frame_bytes

    # Handle trailing silence
    if in_silence:
        silence_dur = current_time_ms - silence_start
        if silence_dur >= min_silence_ms:
            silence_ranges.append((silence_start, current_time_ms))

    return silence_ranges


# ---------------------------------------------------------------------------
# Audio concatenation & splitting
# ---------------------------------------------------------------------------


def concat_audio_frames(frames: list[bytes]) -> bytes:
    """Concatenate multiple PCM audio frames into a single buffer."""
    return b"".join(frames)


def split_into_frames(
    data: bytes,
    frame_duration_ms: int = 20,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> list[bytes]:
    """Split PCM audio data into fixed-duration frames.

    Args:
        data: Raw PCM audio bytes.
        frame_duration_ms: Duration of each frame in ms.
        sample_rate: Sample rate in Hz.
        channels: Number of audio channels.
        sample_width: Bytes per sample.

    Returns:
        List of frame byte buffers.
    """
    frame_bytes = int(sample_rate * frame_duration_ms / 1000) * channels * sample_width
    frames: list[bytes] = []

    for i in range(0, len(data), frame_bytes):
        frame = data[i : i + frame_bytes]
        if len(frame) == frame_bytes:
            frames.append(frame)
        elif len(frame) > 0:
            # Pad last frame with zeros
            frames.append(frame + b"\x00" * (frame_bytes - len(frame)))
    return frames


def pad_or_trim(
    data: bytes,
    target_ms: float,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """Pad or trim audio to exactly target_ms duration.

    Args:
        data: Raw PCM audio bytes.
        target_ms: Target duration in milliseconds.
        sample_rate: Sample rate in Hz.
        channels: Number of channels.
        sample_width: Bytes per sample.

    Returns:
        Audio bytes of exactly the target duration.
    """
    target_bytes = int(sample_rate * target_ms / 1000) * channels * sample_width

    if len(data) > target_bytes:
        return data[:target_bytes]
    elif len(data) < target_bytes:
        return data + b"\x00" * (target_bytes - len(data))
    return data
