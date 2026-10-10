"""
那些/omodul — Layer 3 End-to-End Voice & Vision Modules.

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
from那些.oskill.im.dingtalk import (
    DingTalkGateway,
    make_dingtalk_router,
    format_dingtalk_message,
    format_task_started as format_dingtalk_task_started,
    format_task_result as format_dingtalk_task_result,
    format_error as format_dingtalk_error,
    format_status as format_dingtalk_status,
    format_help as format_dingtalk_help,
)
from那些.oskill.im.wecom import (
    WeComGateway,
    make_wecom_router,
    format_wecom_message,
    format_task_started as format_wecom_task_started,
    format_task_result as format_wecom_task_result,
    format_error as format_wecom_error,
    format_status as format_wecom_status,
    format_help as format_wecom_help,
)
from那些.oskill.im.slack import (
    format_slack_message,
    format_task_started as format_slack_task_started,
    format_task_result as format_slack_task_result,
    format_error as format_slack_error,
    format_status as format_slack_status,
    format_help as format_slack_help,
)
from那些.omodul.daily_brief import (
    DailyBriefConfig,
    DailyBriefInput,
    DailyBriefOutput,
    daily_brief,
    compute_fingerprint_for,
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
    "DailyBriefOutput",
    "daily_brief",
    "compute_fingerprint_for",
]