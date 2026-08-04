"""
AI Agent Collaboration API - P5 Core Capability
Provides multi-agent planning coordination, communication, and task delegation
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hicode.agent_collaboration import AgentRole, create_agent_collaborator

router = APIRouter(prefix="/agent-collaboration", tags=["agent-collaboration"])

# Global collaborator instance (in production, would be per-session or managed by dependency injection)
collaborator = create_agent_collaborator()


class CreateTaskRequest(BaseModel):
    """Request to create a new task"""

    description: str
    agent_role: str  # planner, executor, reviewer, coordinator
    dependencies: list[str] | None = None


class AssignTaskRequest(BaseModel):
    """Request to assign a task to an agent"""

    task_id: str
    agent_id: str


class CompleteTaskRequest(BaseModel):
    """Request to complete a task"""

    task_id: str
    result: Any | None = None
    error: str | None = None


class AddAgentRequest(BaseModel):
    """Request to add an agent"""

    agent_id: str
    role: str
    capabilities: list[str] | None = None


@router.post("/task")
async def create_task(request: CreateTaskRequest) -> dict[str, Any]:
    """Create a new task for collaboration"""
    try:
        # Convert string role to enum
        role_map = {
            "planner": AgentRole.PLANNER,
            "executor": AgentRole.EXECUTOR,
            "reviewer": AgentRole.REVIEWER,
            "coordinator": AgentRole.COORDINATOR,
        }

        role = role_map.get(request.agent_role.lower())
        if not role:
            raise HTTPException(status_code=400, detail=f"Invalid agent role: {request.agent_role}")

        task_id = collaborator.create_task(request.description, role, request.dependencies)

        return {"status": "success", "task_id": task_id, "message": "Task created successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create task: {e!s}")


@router.post("/task/assign")
async def assign_task(request: AssignTaskRequest) -> dict[str, Any]:
    """Assign a task to an agent"""
    try:
        success = collaborator.assign_task(request.task_id, request.agent_id)
        if not success:
            raise HTTPException(status_code=404, detail="Task or agent not found")

        return {
            "status": "success",
            "task_id": request.task_id,
            "agent_id": request.agent_id,
            "message": "Task assigned successfully",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to assign task: {e!s}")


@router.post("/task/complete")
async def complete_task(request: CompleteTaskRequest) -> dict[str, Any]:
    """Mark a task as completed"""
    try:
        success = collaborator.complete_task(request.task_id, request.result, request.error)
        if not success:
            raise HTTPException(status_code=404, detail="Task not found")

        return {
            "status": "success",
            "task_id": request.task_id,
            "message": "Task completed successfully",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to complete task: {e!s}")


@router.get("/task/{task_id}")
async def get_task_status(task_id: str) -> dict[str, Any]:
    """Get status of a specific task"""
    try:
        status = collaborator.get_task_status(task_id)
        if not status:
            raise HTTPException(status_code=404, detail="Task not found")

        return {"status": "success", "task": status}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get task status: {e!s}")


@router.get("/summary")
async def get_collaboration_summary() -> dict[str, Any]:
    """Get summary of current collaboration state"""
    try:
        summary = collaborator.get_collaboration_summary()
        return {"status": "success", "summary": summary}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get collaboration summary: {e!s}")


@router.get("/graph")
async def get_task_graph() -> dict[str, Any]:
    """Get task dependency graph"""
    try:
        graph = collaborator.get_task_graph()
        return {"status": "success", "graph": graph}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get task graph: {e!s}")


@router.post("/agent")
async def add_agent(request: AddAgentRequest) -> dict[str, Any]:
    """Add an agent to the collaboration system"""
    try:
        collaborator.add_agent(request.agent_id, request.role, request.capabilities)

        return {
            "status": "success",
            "agent_id": request.agent_id,
            "message": "Agent added successfully",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to add agent: {e!s}")


@router.delete("/agent/{agent_id}")
async def remove_agent(agent_id: str) -> dict[str, Any]:
    """Remove an agent from the collaboration system"""
    try:
        success = collaborator.remove_agent(agent_id)
        if not success:
            raise HTTPException(status_code=404, detail="Agent not found")

        return {"status": "success", "agent_id": agent_id, "message": "Agent removed successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to remove agent: {e!s}")
