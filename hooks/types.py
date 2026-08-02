from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HookInput:
    """M6 §2 — 切点入参。"""
    point: str                          # pre_tool / post_tool / pre_result / ...
    tool_call: dict[str, Any] | None = None
    result: Any = None
    persona: str = "build"
    cwd: str = "."
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookOutput:
    """M6 §3 — 切点出参。"""
    decision: str = "pass"             # "pass" | "block" | "warn"
    reason: str = ""
    modified_result: Any = None        # post_tool 可改写结果
