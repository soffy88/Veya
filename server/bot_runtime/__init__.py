"""Bot Runtime L0 Closure — Bot lifecycle application service and assembly boundary.

This package unifies BotSpec → AgentDefinition → AgentDeployment → Runtime
Admission → AgentInstance → BotRuntime → AgentSession → GoalRun → Agent Loop
into one executable Bot product chain.

Authority boundaries (frozen, never overridden):
    MasterAgent       = semantic authority
    GoalRun           = durable execution authority
    PermissionEngine  = policy authority
    ComputerSupervisor= physical-computer authority
    IndependentVerifier = acceptance authority
    ExecutorRegistry  = executor capability authority

BotRuntime is assembly / lifecycle boundary only. It does NOT interpret user
intent globally, does NOT replace MasterAgent, does NOT create second
scheduler / task store / acceptance system.

Submodules:
    instance — AgentInstance identity (runtime instance, not execution store)
    engine   — AgentLoopAdapter / AgentEngine protocol and adapters
    runtime  — BotRuntime assembly boundary
    manager  — BotManager lifecycle application service
"""

from .engine import AgentEngine, AgentLoopAdapter, GenesisAgentAdapter
from .instance import AgentInstance, AgentInstanceStatus
from .manager import BotHandle, BotManager
from .runtime import BotRuntime

__all__ = [
    "AgentEngine",
    "AgentInstance",
    "AgentInstanceStatus",
    "AgentLoopAdapter",
    "BotHandle",
    "BotManager",
    "BotRuntime",
    "GenesisAgentAdapter",
]
