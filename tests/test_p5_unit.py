"""
Unit tests for P5: AI Agent Collaboration
Tests core functionality of agent_collaboration.py
"""

from hicode.agent_collaboration import (
    AgentMessage,
    AgentRole,
    AgentTask,
    CollaborationStatus,
    create_agent_collaborator,
)


class TestAgentCollaboration:
    """Test suite for agent collaboration functionality"""

    def setup_method(self):
        """Setup test environment"""
        self.collaborator = create_agent_collaborator("test_coordinator")

    def test_create_agent_collaborator(self):
        """Test creating an agent collaborator"""
        assert self.collaborator is not None
        assert self.collaborator.coordinator_id == "test_coordinator"

        # Check default agents are created
        assert len(self.collaborator.agents) >= 4  # At least 4 default agents
        assert "planner_agent" in self.collaborator.agents
        assert "executor_agent" in self.collaborator.agents
        assert "reviewer_agent" in self.collaborator.agents
        assert "coordinator_agent" in self.collaborator.agents

    def test_add_remove_agent(self):
        """Test adding and removing agents"""
        # Add agent
        self.collaborator.add_agent("custom_planner", AgentRole.PLANNER, ["plan_decomposition"])
        assert "custom_planner" in self.collaborator.agents

        # Get agent status
        status = self.collaborator.get_agent_status("custom_planner")
        assert status is not None
        assert status["role"] == AgentRole.PLANNER
        assert status["capabilities"] == ["plan_decomposition"]

        # Remove agent
        removed = self.collaborator.remove_agent("custom_planner")
        assert removed is True
        assert "custom_planner" not in self.collaborator.agents

        # Try to remove non-existent agent
        removed = self.collaborator.remove_agent("non_existent")
        assert removed is False

    def test_create_task(self):
        """Test creating tasks"""
        # Create simple task
        task_id = self.collaborator.create_task("Simple test task", AgentRole.PLANNER)

        assert task_id is not None
        assert task_id in self.collaborator.tasks

        task = self.collaborator.tasks[task_id]
        assert task.description == "Simple test task"
        assert task.agent_role == AgentRole.PLANNER
        assert task.status == CollaborationStatus.PENDING
        assert len(task.dependencies) == 0

        # Create task with dependencies
        dep_task_id = self.collaborator.create_task("Dependency task", AgentRole.EXECUTOR)

        task_id2 = self.collaborator.create_task(
            "Dependent task", AgentRole.EXECUTOR, [dep_task_id]
        )

        assert task_id2 in self.collaborator.tasks
        assert dep_task_id in self.collaborator.task_dependencies[task_id2]

    def test_assign_task(self):
        """Test assigning tasks to agents"""
        task_id = self.collaborator.create_task("Assignment test task", AgentRole.PLANNER)

        # Assign to existing agent
        success = self.collaborator.assign_task(task_id, "planner_agent")
        assert success is True

        task = self.collaborator.tasks[task_id]
        assert task.status == CollaborationStatus.IN_PROGRESS

        # Try to assign to non-existent agent
        success = self.collaborator.assign_task(task_id, "non_existent_agent")
        assert success is False

        # Try to assign non-existent task
        success = self.collaborator.assign_task("non_existent_task", "planner_agent")
        assert success is False

    def test_complete_task(self):
        """Test completing tasks"""
        task_id = self.collaborator.create_task("Completion test task", AgentRole.PLANNER)

        # Complete successfully
        success = self.collaborator.complete_task(task_id, result="Success!")
        assert success is True

        task = self.collaborator.tasks[task_id]
        assert task.status == CollaborationStatus.COMPLETED
        assert task.result == "Success!"
        assert task.error is None
        assert task.completed_at is not None

        # Complete with error
        task_id2 = self.collaborator.create_task("Error test task", AgentRole.EXECUTOR)

        success = self.collaborator.complete_task(task_id2, error="Something went wrong")
        assert success is True

        task2 = self.collaborator.tasks[task_id2]
        assert task2.status == CollaborationStatus.FAILED
        assert task2.error == "Something went wrong"

    def test_get_task_status(self):
        """Test getting task status"""
        task_id = self.collaborator.create_task("Status test task", AgentRole.PLANNER)

        # Get status of existing task
        status = self.collaborator.get_task_status(task_id)
        assert status is not None
        assert status["task_id"] == task_id
        assert status["description"] == "Status test task"
        assert status["status"] == "pending"

        # Get status of non-existent task
        status = self.collaborator.get_task_status("non_existent_task")
        assert status is None

    def test_get_collaboration_summary(self):
        """Test getting collaboration summary"""
        # Create some tasks
        task1_id = self.collaborator.create_task("Task 1", AgentRole.PLANNER)
        self.collaborator.create_task("Task 2", AgentRole.EXECUTOR)

        # Complete one task
        self.collaborator.complete_task(task1_id, result="Done!")

        summary = self.collaborator.get_collaboration_summary()
        assert summary is not None
        assert summary["total_agents"] >= 4
        assert summary["total_tasks"] == 2
        assert summary["completed_tasks"] == 1
        assert summary["failed_tasks"] == 0
        assert summary["pending_tasks"] == 1

    def test_get_task_graph(self):
        """Test getting task dependency graph"""
        # Create tasks with dependencies
        task1_id = self.collaborator.create_task("Task 1", AgentRole.PLANNER)
        task2_id = self.collaborator.create_task("Task 2", AgentRole.EXECUTOR, [task1_id])

        graph = self.collaborator.get_task_graph()
        assert graph is not None
        assert len(graph["nodes"]) == 2
        assert len(graph["edges"]) == 1

        # Verify edge
        edge = graph["edges"][0]
        assert edge["source"] == task1_id
        assert edge["target"] == task2_id
        assert edge["type"] == "dependency"

    def test_message_creation(self):
        """Test agent message creation"""
        message = AgentMessage(
            sender_id="agent1", receiver_id="agent2", content="Hello, world!", message_type="text"
        )

        assert message.sender_id == "agent1"
        assert message.receiver_id == "agent2"
        assert message.content == "Hello, world!"
        assert message.message_type == "text"
        assert message.timestamp > 0
        assert isinstance(message.message_id, str)
        assert len(message.message_id) > 0

    def test_task_to_dict(self):
        """Test task serialization"""
        task = AgentTask(
            task_id="test_task_123",
            description="Test task",
            agent_role=AgentRole.PLANNER,
            dependencies=["dep1", "dep2"],
        )

        task_dict = task.to_dict()
        assert task_dict["task_id"] == "test_task_123"
        assert task_dict["description"] == "Test task"
        assert task_dict["agent_role"] == "planner"
        assert task_dict["dependencies"] == ["dep1", "dep2"]
        assert task_dict["status"] == "pending"
        assert task_dict["result"] is None
        assert task_dict["error"] is None
