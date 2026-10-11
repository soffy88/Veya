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
from veya.oskill.approval import request_permission_decision
from veya.oskill.vision import (
    analyze_image,
    analyze_image_file,
    analyze_images,
)
from veya.oskill.pure.tool_error_text import (
    explain_tool_error,
    suggest_next_step,
)
from veya.oskill.pure.command_review import (
    build_review_prompt,
    classify_provenance,
    is_human_only,
    parse_review_verdict,
)
from veya.oskill.pure.group_roles import (
    evaluate_group_permission,
    extract_mentions,
    select_replying_bots,
)
from veya.oskill.pure.secret_masking import (
    mask_secret_values,
    redact_secrets_payload,
    secret_env_name,
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
    # NANO-W6: composite permission decision
    "request_permission_decision",
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
    # NANO-W10 Group C: tool error text (item 7a)
    "explain_tool_error",
    "suggest_next_step",
    # NANO-W10 Group C: command review (item 3)
    "build_review_prompt",
    "classify_provenance",
    "is_human_only",
    "parse_review_verdict",
    # NANO-W10 Group C: group roles (addendum P)
    "evaluate_group_permission",
    "extract_mentions",
    "select_replying_bots",
    # NANO-W10 Group C: secret masking (addendum Q)
    "mask_secret_values",
    "redact_secrets_payload",
    "secret_env_name",
    # NANO-W10 Group D: a11y-first observation ladder
    "OBSERVATION_TIERS",
    "ObservationLadderError",
    "ObservationPlan",
    "plan_observation",
    "screenshot_first_plan",
    # NANO-W10 Group D: ADDENDUM R DOM reader
    "MarkdownDocument",
    "extract_main_html",
    "html_to_markdown",
    "is_ssrf_safe_url",
    "read_html",
    # NANO-W10 Group D: ADDENDUM T Search fallback ladder
    "DEFAULT_SEARCH_BACKENDS",
    "SEARCH_FAILURE_NOTICE",
    "SearchHit",
    "SearchLadderError",
    "SearchPlan",
    "format_search_results",
    "merge_search_results",
    "plan_search",
    # NANO-W10 Group D: ADDENDUM V Web-reading channel ladder
    "WEB_READING_CHANNELS",
    "LOGIN_REQUIRED_BACKENDS",
    "WebChannelPlan",
    "plan_web_channel_backend",
    "reorder_channel_backends",
    # NANO-W10 Group D: 主体 7c 6-step tool ladder
    "FULL_TOOL_LADDER_ORDER",
    "full_6step_tool_ladder",
]



from veya.oskill.cli_hub import (
    FULL_TOOL_LADDER_ORDER,
    full_6step_tool_ladder,
)

from veya.oskill.pure.observation_ladder import (
    OBSERVATION_TIERS,
    ObservationLadderError,
    ObservationPlan,
    plan_observation,
    screenshot_first_plan,
)
from veya.oskill.pure.html_to_markdown import (
    MarkdownDocument,
    extract_main_html,
    html_to_markdown,
    is_ssrf_safe_url,
    read_html,
)
from veya.oskill.pure.search_ladder import (
    DEFAULT_SEARCH_BACKENDS,
    SEARCH_FAILURE_NOTICE,
    SearchHit,
    SearchLadderError,
    SearchPlan,
    format_search_results,
    merge_search_results,
    plan_search,
    should_fall_back,
)
from veya.oskill.pure.channel_ladder import (
    CHANNEL_LADDER,
    CHANNEL_MESSAGE_LIMIT,
    ChannelLadderError,
    ChannelPlan,
    LOGIN_REQUIRED_BACKENDS,
    READ_LADDER,
    ReadPlan,
    WEB_READING_CHANNELS,
    WebChannelPlan,
    plan_channel_delivery,
    plan_chunks,
    plan_read,
    plan_web_channel_backend,
    reorder_channel_backends,
)
