import hashlib
import hmac
import json

import pytest
from fastapi import HTTPException

from server.routes.automata import _verified_webhook_payload


class RequestStub:
    def __init__(self, headers):
        self.headers = headers


def signed(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_generic_webhook_requires_configured_secret(monkeypatch):
    monkeypatch.delenv("VEYA_WEBHOOK_SECRET", raising=False)
    with pytest.raises(HTTPException) as error:
        _verified_webhook_payload(RequestStub({}), b"{}")
    assert error.value.status_code == 401


def test_generic_webhook_requires_valid_signature(monkeypatch):
    monkeypatch.setenv("VEYA_WEBHOOK_SECRET", "test-only-not-a-production-secret")
    with pytest.raises(HTTPException) as missing:
        _verified_webhook_payload(RequestStub({}), b"{}")
    assert missing.value.status_code == 401
    with pytest.raises(HTTPException) as invalid:
        _verified_webhook_payload(RequestStub({"X-Veya-Signature": "sha256=invalid"}), b"{}")
    assert invalid.value.status_code == 401


def test_generic_webhook_checks_raw_body_then_accepts_json_dict(monkeypatch):
    secret = "test-only-not-a-production-secret"
    monkeypatch.setenv("VEYA_WEBHOOK_SECRET", secret)
    body = json.dumps({"action": "opened"}, separators=(",", ":")).encode()
    payload = _verified_webhook_payload(
        RequestStub({"X-Veya-Signature": signed(body, secret)}), body
    )
    assert payload == {"action": "opened"}
    with pytest.raises(HTTPException) as error:
        _verified_webhook_payload(
            RequestStub({"X-Veya-Signature": signed(body, secret)}), b'{"action": "changed"}'
        )
    assert error.value.status_code == 401


def test_generic_webhook_rejects_non_object_json(monkeypatch):
    secret = "test-only-not-a-production-secret"
    monkeypatch.setenv("VEYA_WEBHOOK_SECRET", secret)
    body = b"[]"
    with pytest.raises(HTTPException) as error:
        _verified_webhook_payload(RequestStub({"X-Veya-Signature": signed(body, secret)}), body)
    assert error.value.status_code == 400
