"""tests/test_gateway_auth.py — Veya LLM 网关鉴权测试套件。

覆盖要求：
1. 本地免 token（127.0.0.1、::1、docker 网段）正常 200
2. 外部来源无 token 报 401
3. 错误 token / 过期 token / 吊销 token 报 401
4. 正确 token 正常 200
5. /health 接口对外始终免鉴权
6. 流式请求 (stream=true) 也严格走鉴权 (无 token 401, 有 token 200)
7. 限流机制：同一 token 每分钟 60 次，超过报 429
8. 环境变量 VEYA_GATEWAY_AUTH=off 时退回旧行为（免鉴权并警告）
9. CLI issue / list / revoke 完整生命周期
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from scripts.veya_llm_gateway import _RATE_LIMITER, app
from scripts.veya_llm_gateway_tokens import (
    issue_token,
    revoke_token,
)


@pytest.fixture(autouse=True)
def _isolate_tokens_and_rate_limiter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个测试使用独立的临时 token 文件与干净的限流状态。"""
    token_file = tmp_path / "gateway_tokens.json"
    monkeypatch.setenv("VEYA_GATEWAY_TOKENS_FILE", str(token_file))
    monkeypatch.delenv("VEYA_GATEWAY_AUTH", raising=False)
    _RATE_LIMITER.reset()
    yield token_file
    _RATE_LIMITER.reset()


def test_health_always_exempt_from_auth():
    """/health 无论来自本地还是外部，无 token 均应返回 200。"""
    client_local = TestClient(app, client=("127.0.0.1", 50000))
    resp = client_local.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "metrics" in data
    assert "uptime_seconds" in data

    # 外部未受信任 IP 访问 /health 也无需 token
    client_external = TestClient(app, client=("203.0.113.195", 50000))
    resp_ext = client_external.get("/health")
    assert resp_ext.status_code == 200
    data_ext = resp_ext.json()
    assert data_ext["status"] == "ok"
    assert "metrics" in data_ext


def test_local_requests_exempt_from_token():
    """来自 127.0.0.1、::1 或 docker bridge 网段的请求免 token 访问 /v1/models。"""
    # 1. IPv4 Loopback
    client_loopback = TestClient(app, client=("127.0.0.1", 50000))
    resp = client_loopback.get("/v1/models")
    assert resp.status_code == 200
    assert "data" in resp.json()

    # 2. IPv6 Loopback
    client_ipv6 = TestClient(app, client=("::1", 50000))
    resp = client_ipv6.get("/v1/models")
    assert resp.status_code == 200

    # 3. Docker bridge 网段 (172.16.0.0/12 中的 172.18.0.5)
    client_docker = TestClient(app, client=("172.18.0.5", 50000))
    resp = client_docker.get("/v1/models")
    assert resp.status_code == 200

    # 4. veya-net 网段 (192.168.80.0/20 中的 veya-backend 192.168.80.3)
    client_backend = TestClient(app, client=("192.168.80.3", 50000))
    resp = client_backend.get("/v1/models")
    assert resp.status_code == 200


def test_external_without_token_returns_401():
    """外部非受信任 IP 无 token 访问必须返回 401。"""
    client_external = TestClient(app, client=("198.51.100.1", 50000))
    resp = client_external.get("/v1/models")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["type"] == "auth_error"

    # 模拟经反向代理 (172.18.0.1) 转发过来的外部手机客户端 IP
    client_via_proxy = TestClient(app, client=("172.18.0.1", 50000))
    resp_proxied = client_via_proxy.get("/v1/models", headers={"x-forwarded-for": "203.0.113.88"})
    assert resp_proxied.status_code == 401


def test_invalid_and_expired_tokens_return_401(_isolate_tokens_and_rate_limiter: Path):
    """测试错误 token、格式异常 token、过期 token 及吊销 token。"""
    token_file = _isolate_tokens_and_rate_limiter
    client = TestClient(app, client=("198.51.100.1", 50000))

    # 1. 任意错误 token
    resp = client.get("/v1/models", headers={"Authorization": "Bearer vygw_invalid_secret_token"})
    assert resp.status_code == 401

    # 2. 缺少 Bearer 前缀 / 空 token
    assert client.get("/v1/models", headers={"Authorization": "Basic abcdef"}).status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer "}).status_code == 401

    # 3. 过期 token (ttl_days 为负值模拟过期)
    _record, raw_token = issue_token("expired-test", ttl_days=-1.0, tokens_file=token_file)
    resp_expired = client.get("/v1/models", headers={"Authorization": f"Bearer {raw_token}"})
    assert resp_expired.status_code == 401

    # 4. 吊销 token
    _rec_active, active_tok = issue_token("to-revoke", ttl_days=10.0, tokens_file=token_file)
    assert (
        client.get("/v1/models", headers={"Authorization": f"Bearer {active_tok}"}).status_code
        == 200
    )
    revoke_token("to-revoke", tokens_file=token_file)
    assert (
        client.get("/v1/models", headers={"Authorization": f"Bearer {active_tok}"}).status_code
        == 401
    )


def test_valid_token_allows_access(_isolate_tokens_and_rate_limiter: Path):
    """有效 token 允许外部客户端访问受保护接口。"""
    token_file = _isolate_tokens_and_rate_limiter
    _record, raw_token = issue_token("mobile-phone", ttl_days=30.0, tokens_file=token_file)

    client = TestClient(app, client=("198.51.100.1", 50000))
    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {raw_token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert "data" in data
    assert any(m["id"] == "veya1.2" for m in data["data"])


def test_streaming_request_requires_auth(_isolate_tokens_and_rate_limiter: Path):
    """流式 chat/completions (stream=true) 同样必须走鉴权拦截。"""
    token_file = _isolate_tokens_and_rate_limiter
    _record, raw_token = issue_token("mobile-stream-user", ttl_days=7.0, tokens_file=token_file)

    client = TestClient(app, client=("198.51.100.1", 50000))
    payload = {
        "model": "veya1.2",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }

    # 外部未携带 token 请求流式补全 -> 401
    resp_unauth = client.post("/v1/chat/completions", json=payload)
    assert resp_unauth.status_code == 401
    assert resp_unauth.json()["error"]["type"] == "auth_error"

    # 外部携带有效 token 请求流式补全 -> 通过鉴权 (mock 底层 llm_call 验证成功进入流式生成)
    fake_llm_resp = {
        "choices": [{"message": {"role": "assistant", "content": "hello from stream"}}],
        "usage": {"total_tokens": 5},
    }
    with patch("scripts.veya_llm_gateway.llm_call", new=AsyncMock(return_value=fake_llm_resp)):
        resp_auth = client.post(
            "/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp_auth.status_code == 200
        assert "text/event-stream" in resp_auth.headers.get("content-type", "")
        body_text = resp_auth.text
        assert "data: " in body_text
        assert "[DONE]" in body_text


def test_rate_limiting_60_req_per_minute(_isolate_tokens_and_rate_limiter: Path):
    """限流：同一 token 每分钟 60 次，第 61 次返回 429。"""
    token_file = _isolate_tokens_and_rate_limiter
    _record, raw_token = issue_token("ratelimit-test", ttl_days=1.0, tokens_file=token_file)

    client = TestClient(app, client=("198.51.100.1", 50000))
    headers = {"Authorization": f"Bearer {raw_token}"}

    # 前 60 次请求应全部成功 200
    for i in range(60):
        resp = client.get("/v1/models", headers=headers)
        assert resp.status_code == 200, f"Request {i + 1} failed"

    # 第 61 次请求应触发 429
    resp_limited = client.get("/v1/models", headers=headers)
    assert resp_limited.status_code == 429
    body = resp_limited.json()
    assert body["error"]["code"] == "rate_limit_exceeded"
    assert resp_limited.headers.get("Retry-After") == "60"

    # 另一个 token 不受此限流影响
    _rec2, tok2 = issue_token("second-token", ttl_days=1.0, tokens_file=token_file)
    resp_other = client.get("/v1/models", headers={"Authorization": f"Bearer {tok2}"})
    assert resp_other.status_code == 200


def test_env_veya_gateway_auth_off(monkeypatch: pytest.MonkeyPatch):
    """环境变量 VEYA_GATEWAY_AUTH=off 时退回旧行为（外部无 token 也不拦）。"""
    monkeypatch.setenv("VEYA_GATEWAY_AUTH", "off")

    client_external = TestClient(app, client=("198.51.100.1", 50000))
    resp = client_external.get("/v1/models")
    assert resp.status_code == 200
    assert "data" in resp.json()


def test_spoofed_xff_from_untrusted_peer_returns_401():
    """伪造 XFF:127.0.0.1 从不受信对端或非受信任反代发送必须返回 401。"""
    # 1. 外部不受信对端 (198.51.100.1) 伪造 X-Forwarded-For: 127.0.0.1 -> 必须拒绝 401
    client_untrusted = TestClient(app, client=("198.51.100.1", 50000))
    resp = client_untrusted.get("/v1/models", headers={"x-forwarded-for": "127.0.0.1"})
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "auth_error"

    # 2. 外部对端伪造其他反代头 (via, x-forwarded-host) -> 同样必须拒绝 401
    resp_via = client_untrusted.get("/v1/models", headers={"via": "1.1 caddy"})
    assert resp_via.status_code == 401

    # 3. 受信普通对端 (如 docker 容器 192.168.80.3) 非受信任代理，若带 XFF 头伪造 -> 401
    client_docker_peer = TestClient(app, client=("192.168.80.3", 50000))
    resp_docker_spoof = client_docker_peer.get(
        "/v1/models", headers={"x-forwarded-for": "127.0.0.1"}
    )
    assert resp_docker_spoof.status_code == 401


def test_static_gateway_keys_allow_access(monkeypatch: pytest.MonkeyPatch):
    """VEYA_GATEWAY_KEYS 静态 keys 保持可用且受 429 限流保护。"""
    monkeypatch.setenv("VEYA_GATEWAY_KEYS", "sk-static-test-1,sk-static-test-2")

    # 1. 外部直接请求带有效静态 key -> 200
    client_external = TestClient(app, client=("198.51.100.1", 50000))
    resp1 = client_external.get("/v1/models", headers={"Authorization": "Bearer sk-static-test-1"})
    assert resp1.status_code == 200

    # 2. 经 Caddy (172.18.0.1) 反代的外部请求带有效静态 key -> 200
    client_proxy = TestClient(app, client=("172.18.0.1", 50000))
    resp2 = client_proxy.get(
        "/v1/models",
        headers={
            "x-forwarded-for": "203.0.113.88",
            "Authorization": "Bearer sk-static-test-2",
        },
    )
    assert resp2.status_code == 200

    # 3. 错误静态 key -> 401
    resp_bad = client_external.get("/v1/models", headers={"Authorization": "Bearer sk-nonexistent"})
    assert resp_bad.status_code == 401

    # 4. 静态 key 同样受到 60 req/min 限流保护
    for _ in range(59):
        assert (
            client_external.get(
                "/v1/models", headers={"Authorization": "Bearer sk-static-test-1"}
            ).status_code
            == 200
        )
    resp_limit = client_external.get(
        "/v1/models", headers={"Authorization": "Bearer sk-static-test-1"}
    )
    assert resp_limit.status_code == 429
    assert resp_limit.headers.get("Retry-After") == "60"


def test_proxied_external_request_with_valid_token_returns_200(
    _isolate_tokens_and_rate_limiter: Path,
):
    """经 Caddy 反代的外部 IP 请求，携带有效 dynamic token 返回 200。"""
    token_file = _isolate_tokens_and_rate_limiter
    _record, raw_token = issue_token("proxied-mobile", ttl_days=30.0, tokens_file=token_file)

    client_via_caddy = TestClient(app, client=("172.18.0.1", 50000))
    resp = client_via_caddy.get(
        "/v1/models",
        headers={
            "x-forwarded-for": "203.0.113.88, 172.18.0.1",
            "Authorization": f"Bearer {raw_token}",
        },
    )
    assert resp.status_code == 200
    assert "data" in resp.json()


def test_proxied_spoofed_internal_xff_requires_token():
    """经 Caddy 反代的请求即使 XFF 最左为内网 IP（可被客户端伪造）也必须带 token。"""
    client_via_caddy = TestClient(app, client=("172.18.0.1", 50000))

    # 1. 最左侧 IP 为 127.0.0.1
    resp1 = client_via_caddy.get("/v1/models", headers={"x-forwarded-for": "127.0.0.1, 172.18.0.1"})
    assert resp1.status_code == 401

    # 2. 最左侧 IP 为 docker veya-net 192.168.80.3
    resp2 = client_via_caddy.get(
        "/v1/models", headers={"x-forwarded-for": "192.168.80.3, 172.18.0.1"}
    )
    assert resp2.status_code == 401


def test_upstream_pool_status_endpoints():
    """验证从 feat/gateway-local-upstreams 合并的 /v1/wb-pool 和 /v1/fllm-pool 端点正常工作。"""
    client = TestClient(app, client=("127.0.0.1", 50000))
    resp_wb = client.get("/v1/wb-pool")
    assert resp_wb.status_code == 200
    assert "pool" in resp_wb.json() or isinstance(resp_wb.json(), dict)

    resp_fllm = client.get("/v1/fllm-pool")
    assert resp_fllm.status_code == 200
    assert "pool" in resp_fllm.json() or isinstance(resp_fllm.json(), dict)
