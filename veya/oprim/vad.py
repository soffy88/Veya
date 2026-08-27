"""
veya/oprim/vad.py — Voice Activity Detection (Layer 1).

Atomic, stateless VAD operations. Two approaches:
1. Energy-based VAD (pure Python, no deps) — fast, works offline
2. Silero VAD wrapper (optional onnxruntime dep) — ML-based, more accurate

Each function processes a single audio frame and returns a VADResult.
"""

from __future__ import annotations

from typing import Literal

from veya.oprim.audio import bytes_to_int16, compute_rms, linear_to_db
from veya.oprim.types import AudioFrame, VADResult, VADSegment, VADState

# ---------------------------------------------------------------------------
# Energy-based VAD (always available)
# ---------------------------------------------------------------------------


def vad_energy(
    frame: AudioFrame,
    speech_threshold_db: float = -40.0,
    silence_threshold_db: float = -50.0,
    hangover_frames: int = 10,
    _state: dict | None = None,
) -> VADResult:
    """Energy-based voice activity detection on a single audio frame.

    Pure Python, no ML dependencies. Good enough for basic use cases.

    Args:
        frame: Audio frame to analyze.
        speech_threshold_db: dBFS threshold above which = speech.
        silence_threshold_db: dBFS threshold below which = silence.
        hangover_frames: Number of consecutive silence frames before
            transitioning from speech to silence (prevents clipping mid-word).
        _state: Internal state dict for hangover tracking (caller maintains).

    Returns:
        VADResult with state and confidence.

    Example:
        >>> frame = AudioFrame(data=pcm_bytes, sample_rate=16000)
        >>> result = vad_energy(frame)
        >>> if result.is_speech:
        ...     print("User is speaking")
    """
    if _state is None:
        _state = {"hangover_count": 0, "was_speech": False}

    # Compute energy
    samples = bytes_to_int16(frame.data)
    rms_val = compute_rms(samples)
    energy_db = linear_to_db(rms_val)

    # Determine state
    if energy_db > speech_threshold_db:
        _state["hangover_count"] = 0
        state = VADState.STARTING if not _state["was_speech"] else VADState.SPEECH
        _state["was_speech"] = True
        confidence = min(
            1.0,
            (energy_db - silence_threshold_db) / abs(speech_threshold_db - silence_threshold_db),
        )
    elif energy_db < silence_threshold_db:
        if _state["was_speech"]:
            _state["hangover_count"] += 1
            if _state["hangover_count"] >= hangover_frames:
                state = VADState.ENDING
                _state["was_speech"] = False
                _state["hangover_count"] = 0
            else:
                state = VADState.SPEECH  # hangover: still treat as speech
        else:
            state = VADState.SILENCE
        confidence = 1.0 - min(
            1.0,
            (energy_db - silence_threshold_db) / abs(speech_threshold_db - silence_threshold_db),
        )
    else:
        # In between thresholds
        state = VADState.SPEECH if _state["was_speech"] else VADState.SILENCE
        confidence = 0.5

    return VADResult(
        state=state,
        confidence=max(0.0, min(1.0, confidence)),
        energy_db=energy_db,
        frame=frame,
    )


# ---------------------------------------------------------------------------
# Silero VAD wrapper (optional, requires onnxruntime)
# ---------------------------------------------------------------------------

_SILERO_MODEL = None
_SILERO_UTILS = None


def _ensure_silero():
    """Lazy-load silero VAD model. Returns (model, utils) or (None, None)."""
    global _SILERO_MODEL, _SILERO_UTILS
    if _SILERO_MODEL is not None:
        return _SILERO_MODEL, _SILERO_UTILS
    try:
        import numpy as np
        import onnxruntime
    except ImportError:
        return None, None

    try:
        # Silero VAD model URL (publicly available)
        import urllib.request

        model_url = (
            "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
        )
        # Cache model locally
        import os
        import tempfile

        cache_dir = os.path.join(tempfile.gettempdir(), "veya_silero_vad")
        os.makedirs(cache_dir, exist_ok=True)
        model_path = os.path.join(cache_dir, "silero_vad.onnx")

        if not os.path.exists(model_path):
            urllib.request.urlretrieve(model_url, model_path)

        _SILERO_MODEL = onnxruntime.InferenceSession(model_path)
        # Silero utils: get_speech_timestamps helper
        _SILERO_UTILS = np
        return _SILERO_MODEL, _SILERO_UTILS
    except Exception:
        return None, None


def vad_silero(
    frame: AudioFrame,
    threshold: float = 0.5,
    min_speech_duration_ms: int = 250,
    min_silence_duration_ms: int = 100,
    _state: dict | None = None,
) -> VADResult:
    """Silero VAD on a single audio frame (requires onnxruntime).

    Args:
        frame: Audio frame (must be 16000 Hz, mono, 16-bit PCM).
        threshold: Speech probability threshold (0.0-1.0).
        min_speech_duration_ms: Minimum speech duration.
        min_silence_duration_ms: Minimum silence duration.
        _state: Internal state dict for hangover tracking.

    Returns:
        VADResult; falls back to energy-based if silero is unavailable.
    """
    model, _ = _ensure_silero()
    if model is None:
        # Fallback to energy-based VAD
        return vad_energy(frame, _state=_state)

    if _state is None:
        _state = {"hangover_count": 0, "was_speech": False, "audio_buffer": b""}

    import numpy as np

    # Accumulate audio for context window
    _state["audio_buffer"] += frame.data

    # Convert to numpy float32
    samples = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0

    try:
        # Silero expects shape [1, num_samples]
        input_tensor = samples.reshape(1, -1)
        ort_inputs = {model.get_inputs()[0].name: input_tensor}
        ort_outs = model.run(None, ort_inputs)
        speech_prob = float(ort_outs[0][0][0])

        # State classification
        if speech_prob > threshold:
            _state["hangover_count"] = 0
            state = VADState.STARTING if not _state["was_speech"] else VADState.SPEECH
            _state["was_speech"] = True
        else:
            if _state["was_speech"]:
                _state["hangover_count"] += 1
                # Convert hangover frames to ms-equivalent
                hangover_ms = _state["hangover_count"] * frame.duration_ms
                if hangover_ms >= min_silence_duration_ms:
                    state = VADState.ENDING
                    _state["was_speech"] = False
                    _state["hangover_count"] = 0
                else:
                    state = VADState.SPEECH
            else:
                state = VADState.SILENCE

        energy_db = linear_to_db(compute_rms(bytes_to_int16(frame.data)))

        return VADResult(
            state=state,
            confidence=speech_prob,
            energy_db=energy_db,
            frame=frame,
        )
    except Exception:
        # Fallback
        return vad_energy(frame, _state=_state)


# ---------------------------------------------------------------------------
# Unified VAD interface (auto-selects best available)
# ---------------------------------------------------------------------------

VADMode = Literal["auto", "energy", "silero"]


def vad_frame(
    frame: AudioFrame,
    mode: VADMode = "auto",
    threshold_db: float = -40.0,
    silero_threshold: float = 0.5,
    _state: dict | None = None,
) -> VADResult:
    """Unified VAD on a single audio frame — auto-selects best backend.

    Args:
        frame: Audio frame to analyze.
        mode: "auto" (try silero, fallback to energy),
              "energy" (force energy-based),
              "silero" (force silero, fallback to energy).
        threshold_db: Energy threshold in dBFS (for energy mode).
        silero_threshold: Speech probability threshold (for silero mode).
        _state: Internal state for hangover tracking.

    Returns:
        VADResult.
    """
    if mode == "energy":
        return vad_energy(frame, speech_threshold_db=threshold_db, _state=_state)
    elif mode == "silero":
        return vad_silero(frame, threshold=silero_threshold, _state=_state)
    else:  # auto
        return vad_silero(frame, threshold=silero_threshold, _state=_state)


# ---------------------------------------------------------------------------
# VAD segmentation — converts frame-level results to segments
# ---------------------------------------------------------------------------


def build_vad_segments(
    frames: list[tuple[AudioFrame, VADResult]],
    min_speech_ms: int = 200,
    min_silence_ms: int = 300,
) -> list[VADSegment]:
    """Build speech segments from a sequence of VAD frame results.

    Args:
        frames: List of (AudioFrame, VADResult) pairs in chronological order.
        min_speech_ms: Minimum speech duration to create a segment.
        min_silence_ms: Minimum silence duration to break a segment.

    Returns:
        List of VADSegment objects.
    """
    segments: list[VADSegment] = []
    current_segment: VADSegment | None = None
    silence_start_ms: float = 0.0

    for frame, result in frames:
        time_ms = frame.timestamp_ms

        if result.is_speech:
            if current_segment is None:
                current_segment = VADSegment(start_ms=time_ms, end_ms=time_ms)
            current_segment.end_ms = time_ms + frame.duration_ms
            current_segment.frames.append(result)
            current_segment.audio += frame.data
            silence_start_ms = 0.0
        else:
            if current_segment is not None:
                if silence_start_ms == 0.0:
                    silence_start_ms = time_ms
                silence_dur = time_ms - silence_start_ms + frame.duration_ms
                if silence_dur >= min_silence_ms:
                    # End segment if it meets min duration
                    seg_dur = current_segment.end_ms - current_segment.start_ms
                    if seg_dur >= min_speech_ms:
                        segments.append(current_segment)
                    current_segment = None
                    silence_start_ms = 0.0

    # Append final segment
    if current_segment is not None:
        seg_dur = current_segment.end_ms - current_segment.start_ms
        if seg_dur >= min_speech_ms:
            segments.append(current_segment)

    return segments
