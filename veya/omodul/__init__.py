"""
veya/omodul — Layer 3 End-to-End Voice & Vision Modules.

Complete business features built on oskill + oprim.
Each module delivers a full end-to-end capability following the 3O omodul
contract: (config, input, output_dir) -> dict result.

Following the 3O paradigm:
- oprim → atomic ops
- oskill → composite pipelines
- omodul (this) → end-to-end features
- oservi → stateful engine skeletons (uses omodul modules)
"""

# 阶段 4: 注入式流程控制核心 (session_tree / tool_pipeline / agent_loop / evidence_refine)
from veya.omodul.agent_loop import AgentLoop, LoopResult
from veya.omodul.approval_flow import ApprovalFlowConfig, run_approval_flow
from veya.omodul.pairing_flow import PairingFlowConfig, run_pairing_flow

# NANO-W7: morning feed omodul (same-layer re-export from package __init__)
from veya.omodul.daily_brief import (
    DailyBriefConfig,
    DailyBriefInput,
    compute_fingerprint_for,
    daily_brief,
)
from veya.omodul.evidence_refine import EvidenceRefine, RefineResult
from veya.omodul.multimodal_agent import (
    MultiModalAgent,
    MultiModalSessionConfig,
    MultiModalSessionResult,
    MultiModalState,
    run_multimodal_session,
)
from veya.omodul.session_tree import SessionTreeMgr
from veya.omodul.tool_pipeline import ToolPipeline, ToolRunResult, ToolSpec
from veya.omodul.vision_agent import (
    VisionAgent,
    VisionAgentState,
    VisionSessionConfig,
    VisionSessionResult,
    run_vision_analysis,
)
from veya.omodul.voice_agent import (
    VoiceAgent,
    VoiceAgentState,
    VoiceSessionConfig,
    VoiceSessionResult,
    run_voice_conversation,
)
from veya.omodul.auto_review import (
    AutoReviewConfig,
    AutoReviewInput,
    auto_review,
)
from veya.omodul.bot_secrets import (
    BotSecretConfig,
    BotSecretInput,
    bot_secrets_operation,
)


__all__ = [  # noqa: RUF022 - public symbols are grouped by API layer
    # Voice
    "VoiceAgent",
    "VoiceAgentState",
    "VoiceSessionConfig",
    "VoiceSessionResult",
    "run_voice_conversation",
    # Vision
    "VisionAgent",
    "VisionAgentState",
    "VisionSessionConfig",
    "VisionSessionResult",
    "run_vision_analysis",
    # Multi-modal
    "MultiModalAgent",
    "MultiModalSessionConfig",
    "MultiModalSessionResult",
    "MultiModalState",
    "run_multimodal_session",
    # 阶段 4: 注入式流程控制核心
    "AgentLoop",
    "LoopResult",
    "EvidenceRefine",
    "RefineResult",
    "SessionTreeMgr",
    "ToolPipeline",
    "ToolRunResult",
    "ToolSpec",
    # NANO-W7: Morning feed
    "DailyBriefConfig",
    "DailyBriefInput",
    "daily_brief",
    "compute_fingerprint_for",
    # NANO-W10 Group C: command auto-review (item 3)
    "AutoReviewConfig",
    "AutoReviewInput",
    "auto_review",
    # NANO-W10 Group C: bot secrets (addendum Q)
    "BotSecretConfig",
    "BotSecretInput",
    "bot_secrets_operation",
]

