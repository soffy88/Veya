"""Declarative YAML workflows (Buzz borrowing item 4)."""

from .executor import WorkflowExecutor, WorkflowResult
from .loader import load_workflows
from .schema import WorkflowDef, WorkflowStep, WorkflowTrigger

__all__ = [
    "WorkflowDef",
    "WorkflowExecutor",
    "WorkflowResult",
    "WorkflowStep",
    "WorkflowTrigger",
    "load_workflows",
]
