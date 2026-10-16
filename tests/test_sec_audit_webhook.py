import hashlib
import hmac

import pytest
from fastapi import HTTPException

from server.routes.automata import _verified_webhook_payload


class RequestStub:
    def __init__(self, headers):
        self.headers = headers


def _signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_webhook_returns_401_when_secret_is_not_configured(monkeypatch):
    monkeypatch.delenv("VEYA_WEBHOOK_SECRET", raising=False)
    with pytest.raises(HTTPException) as error:
        _verified_webhook_payload(RequestStub({}), b"{}")
    assert error.value.status_code == 401


def test_webhook_requires_hmac_signature_of_exact_raw_body(monkeypatch):
    secret = "test-only-webhook-secret"
    monkeypatch.setenv("VEYA_WEBHOOK_SECRET", secret)
    body = b'{"event":"opened"}'
    assert _verified_webhook_payload(
        RequestStub({"X-Veya-Signature": _signature(body, secret)}), body
    ) == {"event": "opened"}
    with pytest.raises(HTTPException) as error:
        _verified_webhook_payload(
            RequestStub({"X-Veya-Signature": _signature(body, secret)}), b'{"event":"changed"}'
        )
    assert error.value.status_code == 401
