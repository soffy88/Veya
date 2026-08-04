from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Persona:
    name: str
    tool_names: list[str]
    system_prompt: str
    mode: str = "primary"  # "primary" | "subagent"
