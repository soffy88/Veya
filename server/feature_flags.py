"""P1–P3 feature-flag registry with owner and removal metadata.

Flags describe rollout state; the frozen MasterAgent chain never uses them for
semantic routing or tool hiding. Stable runtime capabilities default on.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from config.adapters import feature_flag as _canonical_flag


@dataclass(frozen=True)
class FeatureFlag:
    name: str
    default: bool
    owner: str
    removal_date: str


FLAGS: tuple[FeatureFlag, ...] = (
    FeatureFlag("VEYA_TASK_CENTER_V1", True, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_APPROVAL_GATES", False, "safety", "2027-06-01"),
    FeatureFlag("VEYA_DEVICE_PAIRING", False, "safety", "2027-06-01"),
    FeatureFlag("VEYA_SESSION_UNIFIED_V1", True, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_MEMORY_V2", True, "learning", "2027-01-01"),
    FeatureFlag("VEYA_SKILL_TEACH_V1", True, "learning", "2027-01-01"),
    FeatureFlag("VEYA_RESUME_V2", True, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_TOOL_CONTRACT_V1", True, "safety", "2027-01-01"),
    FeatureFlag("VEYA_EVENT_STORE_V1", True, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_PERMISSION_PROFILES_V1", True, "safety", "2027-01-01"),
    # NANO-W7
    FeatureFlag("VEYA_CHANNELS", False, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_MORNING_FEED", False, "runtime", "2027-01-01"),
    # NANO-W8 flags (default OFF)
    FeatureFlag("VEYA_HANDS", False, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_PROVIDER_REGISTRY", False, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_CLI_HUB", False, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_RUNNER_SELFUPDATE", False, "runtime", "2027-01-01"),
    # NANO-W10 flags (default OFF; appended, never reordered)
    FeatureFlag("VEYA_PROMPT_CACHE", False, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_CONTEXT_STEWARD", False, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_MEMORY_REVIEW", False, "learning", "2027-01-01"),
    FeatureFlag("VEYA_READ_OWN_CHAT", False, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_PERSISTENT_WORKERS", False, "runtime", "2027-01-01"),
    # NANO-W10 Group C flags (default OFF)
    FeatureFlag("VEYA_AUTO_REVIEW", False, "safety", "2027-01-01"),
    FeatureFlag("VEYA_GROUP_ROLES", False, "safety", "2027-01-01"),
    FeatureFlag("VEYA_BOT_SECRETS", False, "safety", "2027-01-01"),
    # NANO-W10 group D (default OFF)
    FeatureFlag("VEYA_A11Y_FIRST", False, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_DOM_READER", False, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_CHANNEL_LADDER", False, "runtime", "2027-01-01"),
    # NANO-W10 Group A flags (spec D3: registered in shared table as single authority)
    FeatureFlag("VEYA_TURN_QUEUE", False, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_WAKE_ROUTER", False, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_LOOP_MODE", False, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_FINISH_CHECKPOINT", False, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_LIVE_STREAM", True, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_TOOL_ERROR_FRIENDLY", True, "safety", "2027-01-01"),
    FeatureFlag("VEYA_LLM_TAP", True, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_SEARCH_FALLBACK", True, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_CHANNEL_STATUS", True, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_BOT_MEMORY_MD", False, "learning", "2027-01-01"),
    # W12 flags (default OFF)
    FeatureFlag("VEYA_BRAIN_BREAKER", False, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_SUPERVISOR", False, "runtime", "2027-01-01"),
    # W13 flag (default OFF; appended after W12, never reordered)
    FeatureFlag("VEYA_REMOTE_CONTROL", False, "runtime", "2027-01-01"),
    # W14: MCP preset pack (5 external MCP servers). Default OFF — presets
    # never launch unless explicitly enabled, matching every other
    # externally-launched integration flag in this table.
    FeatureFlag("VEYA_MCP_PRESETS", False, "runtime", "2027-01-01"),
    # W15: role & skill preset pack (agency-agents / agent-skills, MIT).
    # Default OFF; appended after W14, never reordered.
    FeatureFlag("VEYA_ROLE_SKILL_PRESETS", False, "runtime", "2027-01-01"),
    # W17: SSH host execution backend. Default OFF; appended, never reordered.
    FeatureFlag("VEYA_SSH_HOSTS", False, "runtime", "2027-01-01"),
    # OpenMinis clean-room borrow items (default OFF; appended, never reordered)
    FeatureFlag("VEYA_SKILL_PROGRESSIVE", False, "learning", "2027-01-01"),
    FeatureFlag("VEYA_WORKSPACE_URI", False, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_MEMORY_UI", False, "learning", "2027-01-01"),
    # W17 clean-room: session-scoped ask-once approval cache. Default OFF.
    FeatureFlag("VEYA_APPROVAL_ASK_ONCE", False, "runtime", "2027-01-01"),
    # W16: Project Brain (Hark-style autonomous project mainbrain). Default OFF; appended, never reordered.
    FeatureFlag("VEYA_PROJECT_BRAIN", False, "runtime", "2027-01-01"),
    # FastClaw clean-room borrow items (default OFF; appended, never reordered)
    FeatureFlag("VEYA_IM_SANDBOX_ENFORCE", False, "safety", "2027-01-01"),
    FeatureFlag("VEYA_BOT_PUBLIC_SHARE", False, "safety", "2027-01-01"),
)


def enabled(name: str) -> bool:
    """Read one flag without changing the frozen main-chain decisions.

    Resolved through the ONE configuration authority
    (``config.adapters.feature_flag``: runtime > environment > file >
    default) — no local precedence of its own.  Observable behaviour is
    unchanged: unset → registry default; set → falsy spellings
    (0/false/no/off) disable.
    """
    spec = next((item for item in FLAGS if item.name == name), None)
    if spec is None:
        raise KeyError(name)
    return _canonical_flag(name, spec.default, consumer="server.feature_flags.enabled")


def snapshot() -> list[dict[str, object]]:
    return [{**asdict(spec), "enabled": enabled(spec.name)} for spec in FLAGS]
