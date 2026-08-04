"""
Integration tests for P5: AI Agent Collaboration
Tests API endpoints and coordinator integration
"""

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.coordinator import coordinator


class TestAgentCollaborationIntegration:
    """Test suite for agent collaboration integration"""

    @pytest.fixture
    def client(self):
        """Fixture to create test client"""
        client = TestClient(app)
        yield client
        client.close()

    async def test_create_task_endpoint(self, client):
        """Test creating a task via API endpoint"""
        response = client.post(
            "/agent-collaboration/task",
            json={"description": "API test task", "agent_role": "planner"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "task_id" in data
        assert len(data["task_id"]) > 0

    async def test_assign_task_endpoint(self, client):
        """Test assigning a task via API endpoint"""
        # First create a task
        create_response = client.post(
            "/agent-collaboration/task",
            json={"description": "Assignment test task", "agent_role": "executor"},
        )

        task_id = create_response.json()["task_id"]

        # Then assign it
        assign_response = client.post(
            "/agent-collaboration/task/assign",
            json={"task_id": task_id, "agent_id": "executor_agent"},
        )

        assert assign_response.status_code == 200
        data = assign_response.json()
        assert data["status"] == "success"
        assert data["task_id"] == task_id
        assert data["agent_id"] == "executor_agent"

    async def test_complete_task_endpoint(self, client):
        """Test completing a task via API endpoint"""
        # Create and assign a task first
        create_response = client.post(
            "/agent-collaboration/task",
            json={"description": "Completion test task", "agent_role": "reviewer"},
        )

        task_id = create_response.json()["task_id"]

        _assign_response = client.post(
            "/agent-collaboration/task/assign",
            json={"task_id": task_id, "agent_id": "reviewer_agent"},
        )

        # Then complete it
        complete_response = client.post(
            "/agent-collaboration/task/complete",
            json={"task_id": task_id, "result": "Task completed successfully!"},
        )

        assert complete_response.status_code == 200
        data = complete_response.json()
        assert data["status"] == "success"
        assert data["task_id"] == task_id

    async def test_get_task_status_endpoint(self, client):
        """Test getting task status via API endpoint"""
        # Create a task
        create_response = client.post(
            "/agent-collaboration/task",
            json={"description": "Status test task", "agent_role": "planner"},
        )

        task_id = create_response.json()["task_id"]

        # Get its status
        status_response = client.get(f"/agent-collaboration/task/{task_id}")

        assert status_response.status_code == 200
        data = status_response.json()
        assert data["status"] == "success"
        assert "task" in data
        task_data = data["task"]
        assert task_data["task_id"] == task_id
        assert task_data["description"] == "Status test task"
        assert task_data["status"] == "pending"

    async def test_get_collaboration_summary_endpoint(self, client):
        """Test getting collaboration summary via API endpoint"""
        # Create some tasks
        client.post(
            "/agent-collaboration/task",
            json={"description": "Summary test task 1", "agent_role": "planner"},
        )

        client.post(
            "/agent-collaboration/task",
            json={"description": "Summary test task 2", "agent_role": "executor"},
        )

        # Get summary
        summary_response = client.get("/agent-collaboration/summary")

        assert summary_response.status_code == 200
        data = summary_response.json()
        assert data["status"] == "success"
        assert "summary" in data
        summary = data["summary"]
        assert "total_agents" in summary
        assert "total_tasks" in summary
        assert "completed_tasks" in summary
        assert "failed_tasks" in summary
        assert "pending_tasks" in summary

    async def test_get_task_graph_endpoint(self, client):
        """Test getting task graph via API endpoint"""
        # Create tasks with dependencies
        task1_response = client.post(
            "/agent-collaboration/task",
            json={"description": "Graph test task 1", "agent_role": "planner"},
        )

        task1_id = task1_response.json()["task_id"]

        _task2_response = client.post(
            "/agent-collaboration/task",
            json={
                "description": "Graph test task 2",
                "agent_role": "executor",
                "dependencies": [task1_id],
            },
        )

        # Get graph
        graph_response = client.get("/agent-collaboration/graph")

        assert graph_response.status_code == 200
        data = graph_response.json()
        assert data["status"] == "success"
        assert "graph" in data
        graph = data["graph"]
        assert "nodes" in graph
        assert "edges" in graph
        assert len(graph["nodes"]) >= 2
        assert len(graph["edges"]) >= 1

    async def test_add_remove_agent_endpoints(self, client):
        """Test adding and removing agents via API endpoints"""
        # Add agent
        add_response = client.post(
            "/agent-collaboration/agent",
            json={
                "agent_id": "test_custom_agent",
                "role": "planner",
                "capabilities": ["custom_capability"],
            },
        )

        assert add_response.status_code == 200
        data = add_response.json()
        assert data["status"] == "success"
        assert data["agent_id"] == "test_custom_agent"

        # Remove agent
        remove_response = client.delete("/agent-collaboration/agent/test_custom_agent")

        assert remove_response.status_code == 200
        data = remove_response.json()
        assert data["status"] == "success"
        assert data["agent_id"] == "test_custom_agent"

    async def test_invalid_inputs(self, client):
        """Test error handling for invalid inputs"""
        # Invalid role
        response = client.post(
            "/agent-collaboration/task",
            json={"description": "Invalid role test", "agent_role": "invalid_role"},
        )

        assert response.status_code == 400

        # Non-existent task
        response = client.get("/agent-collaboration/task/non_existent_task")

        assert response.status_code == 404

        # Non-existent agent
        response = client.delete("/agent-collaboration/agent/non_existent_agent")

        assert response.status_code == 404

    async def test_coordinator_methods(self):
        """Test coordinator methods directly"""
        # Test create_collaboration_task
        result = await coordinator.create_collaboration_task("Coordinator test task", "planner")

        assert result["status"] == "success"
        assert "task_id" in result

        task_id = result["task_id"]

        # Test assign_collaboration_task
        result = await coordinator.assign_collaboration_task(task_id, "planner_agent")

        assert result["status"] == "success"
        assert result["task_id"] == task_id

        # Test complete_collaboration_task
        result = await coordinator.complete_collaboration_task(task_id, result="Done!")

        assert result["status"] == "success"
        assert result["task_id"] == task_id

        # Test get_collaboration_task_status
        result = await coordinator.get_collaboration_task_status(task_id)

        assert result["status"] == "success"
        assert "task" in result
        assert result["task"]["task_id"] == task_id

        # Test get_collaboration_summary
        result = await coordinator.get_collaboration_summary()

        assert result["status"] == "success"
        assert "summary" in result

        # Test get_collaboration_task_graph
        result = await coordinator.get_collaboration_task_graph()

        assert result["status"] == "success"
        assert "graph" in result

        # Test add_collaboration_agent
        result = await coordinator.add_collaboration_agent(
            "test_coordinator_agent", "coordinator", ["manage_workflow"]
        )

        assert result["status"] == "success"
        assert result["agent_id"] == "test_coordinator_agent"

        # Test remove_collaboration_agent
        result = await coordinator.remove_collaboration_agent("test_coordinator_agent")

        assert result["status"] == "success"
        assert result["agent_id"] == "test_coordinator_agent"
