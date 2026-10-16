"""Shared test fixture: the admitted test executor counts as installed.

ExecutionAdmission asks ExecutorRegistry whether the admitted executor is
installed and healthy on *this* machine. These suites (and bots by default)
admit ``codex``; whether the codex CLI happens to be installed on the test host
is an environment fact, not the behaviour under test. This autouse fixture
treats ``codex`` as installed and selectable and otherwise runs the real
checks: other executors (e.g. ``grok``) keep their real health verdict,
unknown executors stay denied, and capability matching is still enforced
against codex's real capability projection.
"""

from __future__ import annotations

import pytest

_TEST_EXECUTOR = "codex"


@pytest.fixture(autouse=True)
def _healthy_admission_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.goal_run import execution_admission
    from server.goal_run.execution_recovery import ExecutionRecovery

    real = execution_admission.validate_executor_for_admission

    def _validate(executor_id: str, requested_capabilities=None):
        verdict = real(executor_id, requested_capabilities)
        unhealthy = verdict.reason.startswith("executor_unhealthy") or (
            verdict.reason == "executor_not_selectable"
        )
        if executor_id != _TEST_EXECUTOR or not unhealthy:
            return verdict
        from veya.remote.executor_registry import get_executor_registry

        identity = get_executor_registry().identity(executor_id)
        available = {c.lower() for c in identity.capability.capabilities} | {
            op.lower() for op in identity.capability.supported_operations
        }
        requested = list(requested_capabilities or [])
        matched = [c for c in requested if c.lower() in available]
        missing = [c for c in requested if c.lower() not in available]
        return execution_admission.ExecutorAdmissionVerdict(
            ok=not missing,
            executor_id=executor_id,
            availability="AVAILABLE",
            selectable=True,
            matched_capabilities=matched,
            missing_capabilities=missing,
            reason=(f"executor_capability_mismatch:{','.join(missing)}" if missing else "admitted"),
        )

    real_probe = ExecutionRecovery._probe_provider

    def _probe(self, executor_id: str) -> bool:
        if executor_id == _TEST_EXECUTOR:
            return True
        return real_probe(self, executor_id)

    monkeypatch.setattr(execution_admission, "validate_executor_for_admission", _validate)
    monkeypatch.setattr(ExecutionRecovery, "_probe_provider", _probe)
