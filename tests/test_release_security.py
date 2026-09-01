"""Release security gates for legacy debug surfaces."""

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from server.routes.advanced_visualization import router as advanced_router
from server.routes.debug_guard import require_nonproduction_debug
from server.routes.visualization import router as visualization_router
from server.routes.vscode import router as vscode_router


def test_production_debug_guard_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_EXECUTION_PRODUCTION", "1")
    with pytest.raises(HTTPException) as raised:
        require_nonproduction_debug()
    assert raised.value.status_code == 404


def test_all_debug_routes_use_production_guard() -> None:
    for router in (advanced_router, visualization_router, vscode_router):
        for route in router.routes:
            if not isinstance(route, APIRoute) or "/debug" not in route.path:
                continue
            assert any(
                dependency.call is require_nonproduction_debug
                for dependency in route.dependant.dependencies
            ), route.path
