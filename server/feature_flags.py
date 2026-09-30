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
    FeatureFlag("VEYA_SESSION_UNIFIED_V1", True, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_MEMORY_V2", True, "learning", "2027-01-01"),
    FeatureFlag("VEYA_SKILL_TEACH_V1", True, "learning", "2027-01-01"),
    FeatureFlag("VEYA_RESUME_V2", True, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_TOOL_CONTRACT_V1", True, "safety", "2027-01-01"),
    FeatureFlag("VEYA_EVENT_STORE_V1", True, "runtime", "2027-01-01"),
    FeatureFlag("VEYA_PERMISSION_PROFILES_V1", True, "safety", "2027-01-01"),
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
