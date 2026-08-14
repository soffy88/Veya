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

from veya.omodul.voice_agent import (
    VoiceAgent,
    VoiceAgentState,
    VoiceSessionConfig,
    VoiceSessionResult,
    run_voice_conversation,
)
from veya.omodul.vision_agent import (
    VisionAgent,
    VisionAgentState,
    VisionSessionConfig,
    VisionSessionResult,
    run_vision_analysis,
)
from veya.omodul.multimodal_agent import (
    MultiModalAgent,
    MultiModalSessionConfig,
    MultiModalSessionResult,
    MultiModalState,
    run_multimodal_session,
)
# 阶段 4: 注入式流程控制核心 (session_tree / tool_pipeline / agent_loop / evidence_refine)
from veya.omodul.agent_loop import AgentLoop, LoopResult
from veya.omodul.evidence_refine import EvidenceRefine, RefineResult
from veya.omodul.session_tree import SessionTreeMgr
from veya.omodul.tool_pipeline import ToolPipeline, ToolRunResult, ToolSpec

__all__ = [
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
]
