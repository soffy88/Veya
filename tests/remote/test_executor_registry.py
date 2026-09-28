from __future__ import annotations

import json

from veya.remote.execution_contract import probe_runtime_capability_manifest
from veya.remote.executor_health import ExecutorHealthRegistry
from veya.remote.executor_registry import ExecutorRegistry, ExecutorRuntimeIdentity
from veya.remote.permission_engine import OperationContext, PermissionEngine
from veya.remote.tool_adapter import _worker_model_identity


def test_executor_registry_is_single_identity_authority() -> None:
    registry = ExecutorRegistry(
        overrides={
            "pi": {
                "provider": "test-provider",
                "model": "test-model",
                "launcher": "/tmp/pi",
                "authenticated": True,
                "reachable": True,
            }
        }
    )
    identity = registry.identity("pi")
    assert identity.provider == "test-provider"
    assert registry.identity("PI") is identity


def test_pi_manifest_source_is_runtime_config(tmp_path, monkeypatch) -> None:
    config = tmp_path / "models.json"
    config.write_text(
        json.dumps({"providers": {"runtime-provider": {"models": [{"id": "runtime-model"}]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PI_MODELS_CONFIG", str(config))
    monkeypatch.setenv("PI_SETTINGS_CONFIG", str(tmp_path / "missing-settings.json"))
    identity = ExecutorRegistry().identity("pi")
    assert (identity.provider, identity.model) == ("runtime-provider", "runtime-model")
    assert identity.runtime_source == str(config)


def test_stale_provider_and_model_defaults_are_not_authoritative(tmp_path, monkeypatch) -> None:
    config = tmp_path / "models.json"
    config.write_text(
        json.dumps({"defaultProvider": "current-provider", "defaultModel": "current-model"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PI_MODELS_CONFIG", str(config))
    monkeypatch.setenv("PI_SETTINGS_CONFIG", str(tmp_path / "missing-settings.json"))
    identity = ExecutorRegistry().identity("pi")
    assert identity.provider == "current-provider"
    assert identity.model == "current-model"
    assert identity.model not in {"gpt-5.6-luna", "veya1.2-free", "claude-3-7-sonnet"}


def test_authenticated_and_reachable_are_independent() -> None:
    identity = ExecutorRuntimeIdentity(
        executor_id="future",
        executor_kind="l1_worker",
        provider="p",
        model="m",
        auth_state="MISSING",
        authenticated=False,
        reachable=True,
        launcher="/bin/true",
    )
    assert identity.reachable is True
    assert identity.authenticated is False
    assert identity.status == "UNKNOWN"


def test_permission_context_uses_executor_identity() -> None:
    identity = ExecutorRuntimeIdentity(
        executor_id="pi",
        executor_kind="l1_worker",
        provider="runtime-provider",
        model="runtime-model",
        auth_state="AUTHENTICATED",
        authenticated=True,
        reachable=True,
        launcher="/bin/true",
        status="READY",
    )
    context = OperationContext(
        tool="git",
        operation="git.status",
        executor_identity=identity,
    )
    decision = PermissionEngine().evaluate(context)
    assert decision.allowed
    assert context.executor_identity is identity


def test_future_worker_can_be_registered_without_new_authority() -> None:
    registry = ExecutorRegistry()
    identity = ExecutorRuntimeIdentity(
        executor_id="future-worker",
        executor_kind="l1_worker",
        provider="p",
        model="m",
        auth_state="UNKNOWN",
        reachable=False,
        launcher=None,
    )
    registry.register(identity)
    assert registry.identity("future-worker") == identity


def test_manifest_health_and_dispatch_project_one_identity() -> None:
    registry = ExecutorRegistry()
    health = ExecutorHealthRegistry(executor_registry=registry)
    for worker in ("pi", "codex", "antigravity", "dsh", "opencode", "claude_code"):
        identity = registry.identity(worker)
        manifest = probe_runtime_capability_manifest(worker)
        assert (manifest.provider, manifest.model) == (
            identity.provider or "unknown",
            identity.model or "unknown",
        )
        assert manifest.authenticated == identity.authenticated
        assert health.identity(worker) is identity
        assert _worker_model_identity(worker) == (
            identity.provider or "unknown",
            identity.model or "unknown",
        )


def test_retired_hicode_identity_fails_closed() -> None:
    """Hicode is retired: discovery must fail closed, never return an identity."""
    import pytest

    registry = ExecutorRegistry()
    with pytest.raises(ValueError, match="retired"):
        registry.identity("hicode")
