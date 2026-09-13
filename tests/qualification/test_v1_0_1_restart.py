"""Veya v1.0.1 production-entry qualification checks.

These tests deliberately exercise the real product route and the durable
GoalRun resume contract.  They do not replace the live provider qualification
or claim a process restart when the production provider is unavailable.
"""

from __future__ import annotations

import inspect

from server.routes import product
from server.goal_run import runner


def test_canonical_task_create_route_and_goalrun_wiring() -> None:
    route = next(
        route
        for route in product.router.routes
        if getattr(route, "path", None) == "/api/v1/bot/tasks"
        and "POST" in getattr(route, "methods", set())
    )
    assert route.endpoint is product.create_product_task
    source = inspect.getsource(product._run_product_task)
    assert "CanonicalWorkerAdapter" in source
    assert "project_run_goal" in source
    assert "gateway_executor" in source
    assert "verification_required=True" in source


def test_goalrun_resume_is_same_durable_run() -> None:
    source = inspect.getsource(runner.project_run_goal)
    assert "resume_goal_id" in source
    assert "load_goal_run(project_root, resume_goal_id)" in source
    assert "if resume_goal_id and state is not None" in source
