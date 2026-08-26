"""P1–P3 feature-flag registry with owner and removal metadata.

Flags describe rollout state; the frozen MasterAgent chain never uses them for
semantic routing or tool hiding. Stable runtime capabilities default on.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass


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
    """Read one flag without changing the frozen main-chain decisions."""
    spec = next((item for item in FLAGS if item.name == name), None)
    if spec is None:
        raise KeyError(name)
    raw = os.environ.get(name)
    if raw is None:
        return spec.default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def snapshot() -> list[dict[str, object]]:
    return [{**asdict(spec), "enabled": enabled(spec.name)} for spec in FLAGS]
