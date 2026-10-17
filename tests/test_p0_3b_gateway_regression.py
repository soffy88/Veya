"""Regression tests for P0-3B Host LLM Gateway (:8791).

Covers:
1. /health, /metrics, /v1/metrics output with health status and success rate metrics.
2. Dynamic upstream catalog resolution for local upstreams (e.g. cliproxy, freellmapi).
3. Metrics tracking (total, success, failed, failover, status codes, recent_1h).
4. Failover routing and metric recording.
5. Availability of veya-vl (vision default) and gpt-5.6-luna catalog definitions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
import pytest
from starlette.testclient import TestClient

from scripts.veya_llm_gateway import (
    _CATALOG,
    _LOCAL_UPSTREAMS,
    _STATIC_CATALOG,
    _get_catalog_entry,
    app,
)


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 50000))


def test_health_and_metrics_endpoints(client):
    for path in ("/health", "/metrics", "/v1/metrics"):
        resp = client.get(path)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "uptime_seconds" in data
        assert isinstance(data["uptime_seconds"], (int, float))
        metrics = data["metrics"]
        assert "total_requests" in metrics
        assert "success_requests" in metrics
        assert "failed_requests" in metrics
        assert "success_rate_percent" in metrics
        assert "failover_count" in metrics
        assert "status_codes" in metrics
        assert "recent_1h" in metrics
        assert "total_requests" in metrics["recent_1h"]
        assert "success_rate_percent" in metrics["recent_1h"]


def test_dynamic_catalog_resolution():
    # 1. Registered static entry
    luna = _get_catalog_entry("gpt-5.6-luna")
    assert luna is not None
    assert luna["provider"] == "cliproxy"

    # 2. Registered static cliproxy models
    flash = _get_catalog_entry("cliproxy/gemini-3-flash")
    assert flash is not None
    assert flash["provider"] == "cliproxy"
    assert flash["model"] == "gemini-3-flash"

    # 3. Dynamic resolution of unknown model with valid local upstream prefix
    custom_cliproxy = _get_catalog_entry("cliproxy/brand-new-upstream-model")
    assert custom_cliproxy is not None
    assert custom_cliproxy["provider"] == "cliproxy"
    assert custom_cliproxy["model"] == "brand-new-upstream-model"
    assert custom_cliproxy["endpoint"] == _LOCAL_UPSTREAMS["cliproxy"]

    custom_fllm = _get_catalog_entry("freellmapi/special-free-model")
    assert custom_fllm is not None
    assert custom_fllm["provider"] == "freellmapi"
    assert custom_fllm["endpoint"] == _LOCAL_UPSTREAMS["freellmapi"]

    # 4. Unknown prefix returns None
    unknown = _get_catalog_entry("unknown-provider/xyz")
    assert unknown is None


def test_vision_and_luna_routes_exist():
    # Verify veya-vl is defined in catalog
    vl = _get_catalog_entry("veya-vl")
    assert vl is not None
    assert vl["model"] == "veya-vl"

    # Verify gpt-5.6-luna is defined
    luna = _get_catalog_entry("gpt-5.6-luna")
    assert luna is not None
    assert luna["model"] == "gpt-5.6-luna"


def test_metrics_tracking_on_requests(client):
    # Read initial metrics
    initial = client.get("/health").json()["metrics"]
    initial_total = initial["total_requests"]
    initial_failed = initial["failed_requests"]

    # Request an invalid model -> 400 Bad Request
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "completely-invalid-nonexistent-model", "messages": []},
    )
    assert resp.status_code == 400

    # Verify metrics updated
    after = client.get("/health").json()["metrics"]
    assert after["total_requests"] == initial_total + 1
    assert after["failed_requests"] == initial_failed + 1
    assert after["status_codes"].get("400", 0) >= 1


def test_chat_completions_failover_records_metrics(client, monkeypatch):
    monkeypatch.setenv(
        "VEYA_GATEWAY_FAILOVER",
        "test-failover-model:cliproxy/gemini-3-flash",
    )
    # Inject mock catalog entry for primary
    _CATALOG["test-failover-model"] = {"model": "test-failover-model"}

    mock_succ_resp = {
        "id": "chatcmpl-test",
        "choices": [{"message": {"role": "assistant", "content": "mocked fallback success"}}],
    }

    async def mock_call(entry, messages, tools):
        if entry.get("model") == "test-failover-model":
            return None, (502, "primary failed")
        return mock_succ_resp, None

    with patch("scripts.veya_llm_gateway._call_entry", side_effect=mock_call):
        health_before = client.get("/health").json()["metrics"]
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-failover-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        health_after = client.get("/health").json()["metrics"]
        assert health_after["failover_count"] == health_before["failover_count"] + 1
        assert health_after["success_requests"] == health_before["success_requests"] + 1
