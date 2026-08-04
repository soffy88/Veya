"""
End-to-end tests for P5: AI Agent Collaboration
Tests complete collaborative workflows
"""

from server.coordinator import coordinator


class TestAgentCollaborationE2E:
    """Test suite for agent collaboration end-to-end workflows"""

    async def test_simple_collaborative_workflow(self):
        """Test a simple 3-step collaborative workflow"""
        # Step 1: Planner creates architecture design
        planner_task_id = await coordinator.create_collaboration_task(
            "Design system architecture", "planner"
        )

        assert planner_task_id["status"] == "success"
        planner_task_id = planner_task_id["task_id"]

        # Assign to planner agent
        assign_result = await coordinator.assign_collaboration_task(
            planner_task_id, "planner_agent"
        )
        assert assign_result["status"] == "success"

        # Complete planner task
        complete_result = await coordinator.complete_collaboration_task(
            planner_task_id,
            result={"architecture": "microservices", "components": ["api", "database", "cache"]},
        )
        assert complete_result["status"] == "success"

        # Step 2: Executor implements the design
        executor_task_id = await coordinator.create_collaboration_task(
            "Implement system components", "executor", dependencies=[planner_task_id]
        )

        assert executor_task_id["status"] == "success"
        executor_task_id = executor_task_id["task_id"]

        # Assign to executor agent
        assign_result = await coordinator.assign_collaboration_task(
            executor_task_id, "executor_agent"
        )
        assert assign_result["status"] == "success"

        # Complete executor task
        complete_result = await coordinator.complete_collaboration_task(
            executor_task_id,
            result={"code_files": ["api.py", "database.py", "cache.py"], "tests": ["test_api.py"]},
        )
        assert complete_result["status"] == "success"

        # Step 3: Reviewer validates implementation
        reviewer_task_id = await coordinator.create_collaboration_task(
            "Review code quality and tests", "reviewer", dependencies=[executor_task_id]
        )

        assert reviewer_task_id["status"] == "success"
        reviewer_task_id = reviewer_task_id["task_id"]

        # Assign to reviewer agent
        assign_result = await coordinator.assign_collaboration_task(
            reviewer_task_id, "reviewer_agent"
        )
        assert assign_result["status"] == "success"

        # Complete reviewer task
        complete_result = await coordinator.complete_collaboration_task(
            reviewer_task_id, result={"quality_score": 95, "issues_found": 0, "recommendations": []}
        )
        assert complete_result["status"] == "success"

        # Verify final state
        summary = await coordinator.get_collaboration_summary()
        assert summary["status"] == "success"
        assert summary["summary"]["completed_tasks"] >= 3

        # Get task graph
        graph = await coordinator.get_collaboration_task_graph()
        assert graph["status"] == "success"
        assert len(graph["graph"]["nodes"]) >= 3
        assert len(graph["graph"]["edges"]) >= 2  # At least 2 dependencies

    async def test_parallel_execution_workflow(self):
        """Test parallel execution of independent tasks"""
        # Create multiple independent tasks
        tasks = []

        for i in range(3):
            task_result = await coordinator.create_collaboration_task(
                f"Parallel task {i + 1}", "executor"
            )

            assert task_result["status"] == "success"
            tasks.append(task_result["task_id"])

        # Assign all tasks to different agents (or same agent - doesn't matter for this test)
        for _i, task_id in enumerate(tasks):
            assign_result = await coordinator.assign_collaboration_task(task_id, "executor_agent")
            assert assign_result["status"] == "success"

        # Complete all tasks
        for _i, task_id in enumerate(tasks):
            complete_result = await coordinator.complete_collaboration_task(
                task_id, result=f"Result from task {i + 1}"
            )
            assert complete_result["status"] == "success"

        # Verify all tasks completed
        summary = await coordinator.get_collaboration_summary()
        assert summary["status"] == "success"
        assert summary["summary"]["completed_tasks"] >= 3

    async def test_error_handling_workflow(self):
        """Test workflow with error handling"""
        # Create a task that will fail
        task_result = await coordinator.create_collaboration_task("Task that will fail", "executor")

        assert task_result["status"] == "success"
        task_id = task_result["task_id"]

        # Assign task
        assign_result = await coordinator.assign_collaboration_task(task_id, "executor_agent")
        assert assign_result["status"] == "success"

        # Complete task with error
        complete_result = await coordinator.complete_collaboration_task(
            task_id, error="Execution failed due to timeout"
        )
        assert complete_result["status"] == "success"

        # Verify task is marked as failed
        status_result = await coordinator.get_collaboration_task_status(task_id)
        assert status_result["status"] == "success"
        assert status_result["task"]["status"] == "failed"
        assert status_result["task"]["error"] == "Execution failed due to timeout"

        # Create follow-up task to handle the error
        followup_task_result = await coordinator.create_collaboration_task(
            "Handle previous task failure", "planner", dependencies=[task_id]
        )

        assert followup_task_result["status"] == "success"
        followup_task_id = followup_task_result["task_id"]

        # Complete follow-up task
        assign_result = await coordinator.assign_collaboration_task(
            followup_task_id, "planner_agent"
        )
        assert assign_result["status"] == "success"

        complete_result = await coordinator.complete_collaboration_task(
            followup_task_id, result="Created recovery plan and assigned to new executor"
        )
        assert complete_result["status"] == "success"

        # Verify final state
        summary = await coordinator.get_collaboration_summary()
        assert summary["status"] == "success"
        assert summary["summary"]["failed_tasks"] >= 1
        assert summary["summary"]["completed_tasks"] >= 2

    async def test_complex_dependency_graph(self):
        """Test complex dependency graph with multiple levels"""
        # Level 1: Initial planning
        level1_task = await coordinator.create_collaboration_task(
            "Initial system requirements", "planner"
        )
        level1_task_id = level1_task["task_id"]

        # Level 2: Parallel component designs
        level2_tasks = []
        for component in ["API", "Database", "Frontend"]:
            task = await coordinator.create_collaboration_task(
                f"Design {component} component", "planner", dependencies=[level1_task_id]
            )
            level2_tasks.append(task["task_id"])

        # Level 3: Implementation tasks
        level3_tasks = []
        for i, component_task_id in enumerate(level2_tasks):
            task = await coordinator.create_collaboration_task(
                f"Implement component {i + 1}", "executor", dependencies=[component_task_id]
            )
            level3_tasks.append(task["task_id"])

        # Level 4: Integration testing
        integration_task = await coordinator.create_collaboration_task(
            "Integrate all components", "reviewer", dependencies=level3_tasks
        )
        integration_task_id = integration_task["task_id"]

        # Level 5: Final review
        final_review_task = await coordinator.create_collaboration_task(
            "Final system review", "reviewer", dependencies=[integration_task_id]
        )
        final_review_task_id = final_review_task["task_id"]

        # Complete all tasks in order
        # Level 1
        await coordinator.assign_collaboration_task(level1_task_id, "planner_agent")
        await coordinator.complete_collaboration_task(level1_task_id, result="Requirements defined")

        # Level 2
        for task_id in level2_tasks:
            await coordinator.assign_collaboration_task(task_id, "planner_agent")
            await coordinator.complete_collaboration_task(task_id, result="Component designed")

        # Level 3
        for task_id in level3_tasks:
            await coordinator.assign_collaboration_task(task_id, "executor_agent")
            await coordinator.complete_collaboration_task(task_id, result="Component implemented")

        # Level 4
        await coordinator.assign_collaboration_task(integration_task_id, "reviewer_agent")
        await coordinator.complete_collaboration_task(
            integration_task_id, result="Integration successful"
        )

        # Level 5
        await coordinator.assign_collaboration_task(final_review_task_id, "reviewer_agent")
        await coordinator.complete_collaboration_task(
            final_review_task_id, result="System approved"
        )

        # Verify complex graph structure
        graph = await coordinator.get_collaboration_task_graph()
        assert graph["status"] == "success"
        nodes = graph["graph"]["nodes"]
        edges = graph["graph"]["edges"]

        assert len(nodes) >= 9  # 1 + 3 + 3 + 1 + 1 = 9 tasks
        assert len(edges) >= 10  # Multiple dependencies at each level

        # Verify summary
        summary = await coordinator.get_collaboration_summary()
        assert summary["status"] == "success"
        assert summary["summary"]["completed_tasks"] >= 9

    async def test_custom_agent_capabilities(self):
        """Test custom agent creation and capability-based assignment"""
        # Add custom agents with specific capabilities
        await coordinator.add_collaboration_agent(
            "ml_planner", "planner", ["machine_learning", "neural_networks"]
        )

        await coordinator.add_collaboration_agent(
            "web_developer", "executor", ["react", "nodejs", "mongodb"]
        )

        await coordinator.add_collaboration_agent(
            "security_expert", "reviewer", ["penetration_testing", "security_audit"]
        )

        # Create tasks that match capabilities
        ml_task = await coordinator.create_collaboration_task(
            "Design neural network architecture", "planner"
        )
        web_task = await coordinator.create_collaboration_task(
            "Implement React frontend", "executor"
        )
        security_task = await coordinator.create_collaboration_task(
            "Perform security audit", "reviewer"
        )

        # Assign to appropriate agents
        await coordinator.assign_collaboration_task(ml_task["task_id"], "ml_planner")
        await coordinator.assign_collaboration_task(web_task["task_id"], "web_developer")
        await coordinator.assign_collaboration_task(security_task["task_id"], "security_expert")

        # Complete tasks
        await coordinator.complete_collaboration_task(
            ml_task["task_id"], result="NN architecture designed"
        )
        await coordinator.complete_collaboration_task(
            web_task["task_id"], result="React app implemented"
        )
        await coordinator.complete_collaboration_task(
            security_task["task_id"], result="Security audit completed"
        )

        # Verify assignments worked
        ml_status = await coordinator.get_collaboration_task_status(ml_task["task_id"])
        assert ml_status["status"] == "success"
        assert ml_status["task"]["status"] == "completed"

        # Clean up custom agents
        await coordinator.remove_collaboration_agent("ml_planner")
        await coordinator.remove_collaboration_agent("web_developer")
        await coordinator.remove_collaboration_agent("security_expert")
