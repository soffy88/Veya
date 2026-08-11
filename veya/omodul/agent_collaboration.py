"""
AI Agent Collaboration Module - P5 Core Capability
Functionality: Multi-agent planning coordination, communication, and task delegation
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any


class AgentRole(StrEnum):
    """Agent role types"""

    PLANNER = "planner"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    COORDINATOR = "coordinator"


class CollaborationStatus(StrEnum):
    """Collaboration status"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentMessage:
    """Message between agents"""

    def __init__(self, sender_id: str, receiver_id: str, content: str, message_type: str = "text"):
        self.message_id = str(uuid.uuid4())
        self.sender_id = sender_id
        self.receiver_id = receiver_id
        self.content = content
        self.message_type = message_type
        self.timestamp = time.time()
        self.metadata = {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender_id": self.sender_id,
            "receiver_id": self.receiver_id,
            "content": self.content,
            "message_type": self.message_type,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


class AgentTask:
    """Task for an agent"""

    def __init__(
        self,
        task_id: str,
        description: str,
        agent_role: AgentRole,
        dependencies: list[str] | None = None,
    ):
        self.task_id = task_id
        self.description = description
        self.agent_role = agent_role
        self.dependencies = dependencies or []
        self.status = CollaborationStatus.PENDING
        self.created_at = time.time()
        self.completed_at = None
        self.result = None
        self.error = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "agent_role": self.agent_role.value,
            "dependencies": self.dependencies,
            "status": self.status.value,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error": self.error,
        }


class AgentCollaborator:
    """
    AI Agent Collaboration Coordinator

    Functionality:
    1. Multi-agent planning coordination
    2. Task delegation and dependency management
    3. Agent communication and messaging
    4. Status tracking and reporting
    """

    def __init__(self, coordinator_id: str = "default"):
        self.coordinator_id = coordinator_id
        self.agents: dict[str, dict[str, Any]] = {}  # agent_id -> agent_info
        self.tasks: dict[str, AgentTask] = {}
        self.messages: list[AgentMessage] = []
        self.task_dependencies: dict[str, list[str]] = {}  # task_id -> list of dependent tasks
        self.task_status: dict[str, CollaborationStatus] = {}

        # Initialize default agents
        self._initialize_default_agents()

    def _initialize_default_agents(self):
        """Initialize default agents"""
        self.add_agent("planner_agent", AgentRole.PLANNER)
        self.add_agent("executor_agent", AgentRole.EXECUTOR)
        self.add_agent("reviewer_agent", AgentRole.REVIEWER)
        self.add_agent("coordinator_agent", AgentRole.COORDINATOR)

    def add_agent(self, agent_id: str, role: AgentRole, capabilities: list[str] | None = None):
        """Add an agent to the collaboration system"""
        if capabilities is None:
            capabilities = []

        self.agents[agent_id] = {
            "agent_id": agent_id,
            "role": role,
            "capabilities": capabilities,
            "status": "online",
            "last_seen": time.time(),
        }

    def remove_agent(self, agent_id: str) -> bool:
        """Remove an agent from the collaboration system"""
        if agent_id in self.agents:
            del self.agents[agent_id]
            return True
        return False

    def create_task(
        self, description: str, agent_role: AgentRole, dependencies: list[str] | None = None
    ) -> str:
        """Create a new task for collaboration"""
        task_id = str(uuid.uuid4())
        task = AgentTask(task_id, description, agent_role, dependencies)
        self.tasks[task_id] = task

        # Track dependencies
        if dependencies:
            self.task_dependencies[task_id] = dependencies

        return task_id

    def assign_task(self, task_id: str, agent_id: str) -> bool:
        """Assign a task to an agent"""
        if task_id not in self.tasks:
            return False

        if agent_id not in self.agents:
            return False

        task = self.tasks[task_id]
        task.agent_id = agent_id
        task.status = CollaborationStatus.IN_PROGRESS

        # Send assignment message
        message = AgentMessage(
            sender_id="coordinator_agent",
            receiver_id=agent_id,
            content=f"Task assigned: {task.description}",
            message_type="assignment",
        )
        self.messages.append(message)

        return True

    def complete_task(
        self, task_id: str, result: Any | None = None, error: str | None = None
    ) -> bool:
        """Mark a task as completed"""
        if task_id not in self.tasks:
            return False

        task = self.tasks[task_id]
        task.result = result
        task.error = error

        if error:
            task.status = CollaborationStatus.FAILED
        else:
            task.status = CollaborationStatus.COMPLETED
            task.completed_at = time.time()

        # Notify other agents about completion
        self._notify_task_completion(task_id, result, error)

        return True

    def _notify_task_completion(self, task_id: str, result: Any, error: str):
        """Notify other agents about task completion"""
        # Send completion message to all agents
        for agent_id in self.agents:
            if agent_id != "coordinator_agent":
                message = AgentMessage(
                    sender_id="coordinator_agent",
                    receiver_id=agent_id,
                    content=f"Task {task_id} completed",
                    message_type="completion",
                )
                message.metadata["task_id"] = task_id
                message.metadata["result"] = result
                message.metadata["error"] = error
                self.messages.append(message)

    def get_task_status(self, task_id: str) -> dict[str, Any] | None:
        """Get status of a task"""
        if task_id not in self.tasks:
            return None

        return self.tasks[task_id].to_dict()

    def get_agent_status(self, agent_id: str) -> dict[str, Any] | None:
        """Get status of an agent"""
        return self.agents.get(agent_id)

    def get_collaboration_summary(self) -> dict[str, Any]:
        """Get summary of current collaboration state"""
        total_tasks = len(self.tasks)
        completed_tasks = sum(
            1 for task in self.tasks.values() if task.status == CollaborationStatus.COMPLETED
        )
        failed_tasks = sum(
            1 for task in self.tasks.values() if task.status == CollaborationStatus.FAILED
        )

        return {
            "coordinator_id": self.coordinator_id,
            "total_agents": len(self.agents),
            "total_tasks": total_tasks,
            "completed_tasks": completed_tasks,
            "failed_tasks": failed_tasks,
            "pending_tasks": total_tasks - completed_tasks - failed_tasks,
            "active_messages": len(self.messages),
        }

    def get_task_graph(self) -> dict[str, Any]:
        """Get task dependency graph"""
        # Return a simple graph representation
        nodes = [
            {
                "id": task_id,
                "label": task.description[:30] + "..."
                if len(task.description) > 30
                else task.description,
                "status": task.status.value,
                "role": task.agent_role.value,
            }
            for task_id, task in self.tasks.items()
        ]

        edges = []
        for task_id, dependencies in self.task_dependencies.items():
            for dep_id in dependencies:
                edges.append({"source": dep_id, "target": task_id, "type": "dependency"})

        return {"nodes": nodes, "edges": edges}


# Convenience functions
def create_agent_collaborator(coordinator_id: str = "default") -> AgentCollaborator:
    """Create an agent collaboration coordinator"""
    return AgentCollaborator(coordinator_id)


if __name__ == "__main__":
    # Test the collaboration module
    print("=== Testing Agent Collaboration ===")

    collaborator = create_agent_collaborator("test_coordinator")

    # Add custom agents
    collaborator.add_agent("custom_planner", AgentRole.PLANNER, ["plan_decomposition"])
    collaborator.add_agent("custom_executor", AgentRole.EXECUTOR, ["code_generation"])

    # Create tasks
    task1_id = collaborator.create_task(
        "Decompose complex problem into subtasks", AgentRole.PLANNER
    )

    task2_id = collaborator.create_task(
        "Generate implementation code", AgentRole.EXECUTOR, dependencies=[task1_id]
    )

    print(f"Created tasks: {task1_id[:8]}..., {task2_id[:8]}...")

    # Assign tasks
    collaborator.assign_task(task1_id, "custom_planner")
    collaborator.assign_task(task2_id, "custom_executor")

    # Complete tasks
    collaborator.complete_task(task1_id, result="Subtasks created: [design, implement, test]")
    collaborator.complete_task(task2_id, result="Code generated successfully")

    # Get summary
    summary = collaborator.get_collaboration_summary()
    print(f"Collaboration summary: {summary}")

    # Get task graph
    graph = collaborator.get_task_graph()
    print(f"Task graph: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")

    # Get task status
    status = collaborator.get_task_status(task1_id)
    print(f"Task 1 status: {status['status']}")
