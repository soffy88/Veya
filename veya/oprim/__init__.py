"""
veya/oprim — Layer 1 Atomic Operations for Audio, Video, and VAD.

Stateless, pure-function operations. Each function completes a single task
independently and has no side effects. No external API calls, no I/O beyond
what's specified.

Following the 3O paradigm:
- oprim (this) → atomic ops
- oskill → composite pipelines built on oprim
- omodul → end-to-end features built on oskill
"""

from veya.oprim.audio import (
    bytes_to_int16,
    compute_rms,
    concat_audio_frames,
    db_to_linear,
    detect_silence_segments,
    float32_to_int16,
    int16_to_bytes,
    int16_to_float32,
    is_silence_frame,
    linear_to_db,
    pad_or_trim,
    pcm_to_wav,
    resample_audio,
    split_into_frames,
    wav_to_pcm,
)
from veya.oprim.types import (
    AudioConfig,
    AudioFormat,
    AudioFrame,
    ImageFormat,
    ImageFrame,
    TranscriptionResult,
    TranscriptionWord,
    TurnDecision,
    TurnState,
    VADResult,
    VADSegment,
    VADState,
    VideoFrameSample,
    VisionResult,
)
from veya.oprim.vad import (
    VADMode,
    build_vad_segments,
    vad_energy,
    vad_frame,
    vad_silero,
)
from veya.oprim.video import (
    create_image_frame,
    decode_image_base64,
    detect_format_from_path,
    detect_image_format,
    encode_image_base64,
    encode_image_bytes_base64,
    extract_video_frame_at,
    image_to_data_uri,
    load_image_frame,
    parse_data_uri,
    resize_image_simple,
    sample_video_frames,
    validate_image,
)

__all__ = [
    # Types
    "AudioConfig",
    "AudioFormat",
    "AudioFrame",
    "ImageFormat",
    "ImageFrame",
    "TranscriptionResult",
    "TranscriptionWord",
    "TurnDecision",
    "TurnState",
    "VADMode",
    "VADResult",
    "VADSegment",
    "VADState",
    "VideoFrameSample",
    "VisionResult",
    # Audio ops
    "bytes_to_int16",
    "compute_rms",
    "concat_audio_frames",
    "db_to_linear",
    "detect_silence_segments",
    "float32_to_int16",
    "int16_to_bytes",
    "int16_to_float32",
    "is_silence_frame",
    "linear_to_db",
    "pad_or_trim",
    "pcm_to_wav",
    "resample_audio",
    "split_into_frames",
    "wav_to_pcm",
    # Video ops
    "create_image_frame",
    "decode_image_base64",
    "detect_format_from_path",
    "detect_image_format",
    "encode_image_base64",
    "encode_image_bytes_base64",
    "extract_video_frame_at",
    "image_to_data_uri",
    "load_image_frame",
    "parse_data_uri",
    "resize_image_simple",
    "sample_video_frames",
    "validate_image",
    # VAD ops
    "build_vad_segments",
    "vad_energy",
    "vad_frame",
    "vad_silero",
]
