from __future__ import annotations

import pytest

from server.authorization import (
    AuthorizationDenied,
    Delegation,
    Principal,
    Resource,
    authorize,
    authorize_tool,
    redact,
)


def _principal(**kwargs):
    return Principal(
        subject_id=kwargs.get("subject_id", "user-a"),
        tenant_id=kwargs.get("tenant_id", "tenant-a"),
        bot_id=kwargs.get("bot_id", "bot-a"),
        scopes=frozenset(kwargs.get("scopes", ())),
    )


def _resource(**kwargs):
    return Resource(
        kind=kwargs.get("kind", "goal_run"),
        owner_id=kwargs.get("owner_id", "user-a"),
        tenant_id=kwargs.get("tenant_id", "tenant-a"),
        bot_id=kwargs.get("bot_id", "bot-a"),
    )


def test_default_deny_and_same_owner_access() -> None:
    authorize(_principal(), "read", _resource())
    with pytest.raises(AuthorizationDenied):
        authorize(_principal(scopes=()), "write", _resource(owner_id="user-b"))
    with pytest.raises(AuthorizationDenied):
        authorize(_principal(tenant_id="tenant-b"), "read", _resource())


def test_delegation_cannot_become_a_confused_deputy() -> None:
    grant = Delegation(
        source_bot_id="bot-a",
        target_bot_id="bot-b",
        delegate_id="delegate-1",
        allowed_scopes=frozenset({"read"}),
    )
    authorize(
        _principal(subject_id="user-b", bot_id="bot-b"),
        "read",
        _resource(owner_id="user-b", bot_id="bot-b"),
        delegation=grant,
    )
    with pytest.raises(AuthorizationDenied):
        authorize(
            _principal(subject_id="user-b", bot_id="bot-b"),
            "write",
            _resource(owner_id="user-b", bot_id="bot-b"),
            delegation=grant,
        )
    with pytest.raises(AuthorizationDenied):
        authorize(
            _principal(subject_id="user-b", bot_id="bot-b"),
            "read",
            _resource(owner_id="user-a", bot_id="bot-a"),
            delegation=grant,
        )


def test_physical_tool_requires_explicit_gateway_and_scope() -> None:
    with pytest.raises(AuthorizationDenied):
        authorize_tool(_principal(), "browser_run", physical=True, gateway_bound=False)
    with pytest.raises(AuthorizationDenied):
        authorize_tool(_principal(), "browser_run", physical=True, gateway_bound=True)
    authorize_tool(
        _principal(scopes={"browser_run"}),
        "browser_run",
        physical=True,
        gateway_bound=True,
    )


@pytest.mark.asyncio
async def test_action_gateway_applies_explicit_principal_before_physical_call():
    from server.action_gateway_adapter import ActionGatewayAdapter

    gateway = ActionGatewayAdapter(
        authorization_principal=_principal(scopes={"browser_run"}),
    )
    result = await gateway.execute(
        "browser_run",
        {},
        lambda **_kwargs: {"ok": True},
        side_effect="pure_read",
    )
    assert result["result"] == {"ok": True}

    denied = ActionGatewayAdapter(authorization_principal=_principal())
    with pytest.raises(AuthorizationDenied):
        await denied.execute(
            "browser_run",
            {},
            lambda **_kwargs: {"ok": True},
            side_effect="network_write",
        )


def test_secret_projection_redacts_before_untrusted_sinks() -> None:
    projected = redact({"token": "secret", "nested": [{"password": "secret"}], "ok": 1})
    assert projected == {
        "token": "[REDACTED]",
        "nested": [{"password": "[REDACTED]"}],
        "ok": 1,
    }
