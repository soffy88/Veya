import pytest
from fastapi import HTTPException

from server.routes.advanced_visualization import router
from server.routes.debug_guard import require_nonproduction_debug


def test_expression_evaluation_http_route_is_removed():
    assert all(getattr(route, "path", "") != "/advanced-visualization/debug/expression" for route in router.routes)


def test_debug_guard_defaults_to_production(monkeypatch):
    monkeypatch.delenv("VEYA_EXECUTION_PRODUCTION", raising=False)
    with pytest.raises(HTTPException) as exc:
        require_nonproduction_debug()
    assert exc.value.status_code == 404


def test_debug_guard_requires_explicit_nonproduction_opt_in(monkeypatch):
    monkeypatch.setenv("VEYA_EXECUTION_PRODUCTION", "0")
    require_nonproduction_debug()
