"""
veya/oskill — Layer 2 Composite Voice & Vision Pipelines.

Composite algorithms and workflows built on oprim atomic operations.
Each skill orchestrates multiple oprim ops + optional external provider APIs
to deliver a complete capability.

Following the 3O paradigm:
- oprim → atomic ops (no deps beyond stdlib)
- oskill (this) → composite pipelines (depends on oprim + provider APIs)
- omodul → end-to-end features (depends on oskill)
"""

from veya.oskill.audio_io import (
    AudioDevice,
    AudioDeviceType,
    AudioPipeline,
    MemoryAudioSink,
    MemoryAudioSource,
    create_audio_pipeline,
    list_audio_devices,
)
from veya.oskill.stt import (
    speech_to_text,
    speech_to_text_streaming,
    transcribe_file,
)
from veya.oskill.tts import (
    list_voices,
    text_to_speech,
    text_to_speech_streaming,
)
from veya.oskill.turn_detection import (
    EndpointingConfig,
    InterruptionConfig,
    TurnDetector,
    TurnHandlingConfig,
    detect_turn_end,
)
from veya.oskill.vision import (
    analyze_image,
    analyze_image_file,
    analyze_images,
)

__all__ = [
    # STT
    "speech_to_text",
    "speech_to_text_streaming",
    "transcribe_file",
    # TTS
    "text_to_speech",
    "text_to_speech_streaming",
    "list_voices",
    # Turn detection
    "TurnDetector",
    "TurnHandlingConfig",
    "EndpointingConfig",
    "InterruptionConfig",
    "detect_turn_end",
    # Vision
    "analyze_image",
    "analyze_image_file",
    "analyze_images",
    # Audio I/O
    "AudioPipeline",
    "AudioDevice",
    "AudioDeviceType",
    "MemoryAudioSource",
    "MemoryAudioSink",
    "create_audio_pipeline",
    "list_audio_devices",
]
