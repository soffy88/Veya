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
from veya.oskill.cli_hub import (
    cli_hub_tool_ladder,
    cli_hub_verify_output,
)
from veya.oskill.hands import (
    HandsAction,
    HandsOskillError,
    LLMCaller,
    hands_approval_gate,
    hands_operator_loop_step,
    hands_pause_resume,
)
from veya.oskill.runner_ops import (
    runner_safe_to_restart,
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

__all__ = [  # noqa: RUF022 - public symbols are grouped by API layer
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
    # Hands (VEYA_HANDS)
    "HandsAction",
    "HandsOskillError",
    "LLMCaller",
    "hands_approval_gate",
    "hands_operator_loop_step",
    "hands_pause_resume",
    # CLI-Hub (VEYA_CLI_HUB)
    "cli_hub_tool_ladder",
    "cli_hub_verify_output",
    # Runner operations (VEYA_RUNNER_SELFUPDATE)
    "runner_safe_to_restart",
]
