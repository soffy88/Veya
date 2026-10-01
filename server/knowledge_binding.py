"""P1-06 Knowledge Binding — scope-based knowledge applicability.

Context applicability considers: semantic relevance, scope compatibility,
authority, freshness, binding — not semantic similarity alone.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class BindingScope(StrEnum):
    GLOBAL = "GLOBAL"
    PROJECT = "PROJECT"
    REPOSITORY = "REPOSITORY"
    WORKSPACE = "WORKSPACE"
    GOAL = "GOAL"
    SESSION = "SESSION"
    AGENT = "AGENT"
    EXECUTION = "EXECUTION"


@dataclass(frozen=True)
class KnowledgeBinding:
    """A binding between knowledge and a scope."""

    binding_id: str
    knowledge_id: str
    scope: BindingScope
    project_id: str | None = None
    repository_id: str | None = None
    path_selector: str | None = None
    goal_type: str | None = None
    skill_id: str | None = None
    priority: int = 0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["scope"] = self.scope.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeBinding:
        data = dict(data)
        if isinstance(data.get("scope"), str):
            data["scope"] = BindingScope(data["scope"])
        return cls(**data)


def new_binding(
    knowledge_id: str,
    scope: BindingScope,
    *,
    project_id: str | None = None,
    repository_id: str | None = None,
    path_selector: str | None = None,
    goal_type: str | None = None,
    skill_id: str | None = None,
    priority: int = 0,
) -> KnowledgeBinding:
    """Create a new knowledge binding."""
    return KnowledgeBinding(
        binding_id=str(uuid.uuid4()),
        knowledge_id=knowledge_id,
        scope=scope,
        project_id=project_id,
        repository_id=repository_id,
        path_selector=path_selector,
        goal_type=goal_type,
        skill_id=skill_id,
        priority=priority,
    )


def binding_applies(
    binding: KnowledgeBinding,
    *,
    context_scope: BindingScope,
    project_id: str | None = None,
    repository_id: str | None = None,
    goal_type: str | None = None,
    skill_id: str | None = None,
) -> bool:
    """Check if a binding applies to a given context."""
    if binding.scope is not context_scope:
        return False
    if binding.project_id is not None and binding.project_id != project_id:
        return False
    if binding.repository_id is not None and binding.repository_id != repository_id:
        return False
    if binding.goal_type is not None and binding.goal_type != goal_type:
        return False
    if binding.skill_id is not None and binding.skill_id != skill_id:
        return False
    return True
