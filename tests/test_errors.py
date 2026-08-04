"""
Tests for veya.errors — structured error taxonomy.

Validates:
  - All concrete exceptions inherit from HicodeError
  - Code constants are unique and uppercase
  - to_dict() produces serialisable output
  - veya_error() factory works
  - Severity enum values are valid
"""

from __future__ import annotations

import pytest

from veya.errors import (
    CheckpointError,
    ConfigLoadError,
    # Config
    ConfigValidationError,
    CoordinatorDAGError,
    # Coordinator
    CoordinatorTimeoutError,
    HicodeError,
    HookAbortError,
    # Hooks
    HookExecutionError,
    ModelAPIError,
    # Model
    ModelNotFoundError,
    # Permission
    PermissionDeniedError,
    PermissionScopeError,
    RegistryDuplicateError,
    # Registry
    RegistryNotFoundError,
    SandboxFileAccessError,
    # Sandbox
    SandboxInitError,
    SandboxResourceExceeded,
    # Session
    SessionExpiredError,
    SessionStateError,
    Severity,
    # Streaming
    StreamConnectionError,
    StreamEncodingError,
    # Tools
    ToolExecutionError,
    ToolNotAvailableError,
    ToolValidationError,
    # Factory
    veya_error,
)

# ---------------------------------------------------------------------------
# Registry of all concrete error classes (for parametrisation)
# ---------------------------------------------------------------------------
ALL_ERRORS = [
    CoordinatorTimeoutError,
    CoordinatorDAGError,
    CheckpointError,
    SandboxInitError,
    SandboxResourceExceeded,
    SandboxFileAccessError,
    RegistryNotFoundError,
    RegistryDuplicateError,
    StreamConnectionError,
    StreamEncodingError,
    ToolExecutionError,
    ToolValidationError,
    ToolNotAvailableError,
    ConfigValidationError,
    ConfigLoadError,
    SessionExpiredError,
    SessionStateError,
    ModelNotFoundError,
    ModelAPIError,
    HookExecutionError,
    HookAbortError,
    PermissionDeniedError,
    PermissionScopeError,
]


# ---------------------------------------------------------------------------
# Inheritance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", ALL_ERRORS)
def test_all_errors_inherit_from_veya_error(cls):
    assert issubclass(cls, HicodeError)


# ---------------------------------------------------------------------------
# Uniqueness of error codes
# ---------------------------------------------------------------------------


def test_error_codes_are_unique():
    codes = [cls.code for cls in ALL_ERRORS]
    assert len(codes) == len(set(codes)), (
        f"Duplicate codes: {[c for c in codes if codes.count(c) > 1]}"
    )


def test_error_codes_are_uppercase():
    for cls in ALL_ERRORS:
        assert cls.code == cls.code.upper(), f"{cls.__name__}.code is not uppercase: {cls.code}"


# ---------------------------------------------------------------------------
# Base exception defaults
# ---------------------------------------------------------------------------


def test_veya_error_defaults():
    err = HicodeError("something broke")
    assert err.code == "VEYA_UNKNOWN"
    assert err.severity == Severity.ERROR
    assert err.component == "veya"
    assert err.message == "something broke"
    assert err.context == {}


# ---------------------------------------------------------------------------
# to_dict() serialisation
# ---------------------------------------------------------------------------


def test_to_dict_returns_serialisable_dict():
    err = HicodeError(
        "test",
        code="TEST_CODE",
        severity=Severity.WARNING,
        component="test",
        context={"key": "value"},
    )
    d = err.to_dict()
    assert d["error"] is True
    assert d["code"] == "TEST_CODE"
    assert d["severity"] == "warning"
    assert d["component"] == "test"
    assert d["message"] == "test"
    assert d["context"] == {"key": "value"}

    # Ensure values are JSON-serialisable
    import json

    assert json.dumps(d)  # no TypeError


# ---------------------------------------------------------------------------
# Severity enum
# ---------------------------------------------------------------------------


def test_severity_values():
    assert Severity.INFO.value == "info"
    assert Severity.WARNING.value == "warning"
    assert Severity.ERROR.value == "error"
    assert Severity.CRITICAL.value == "critical"


def test_severity_from_string():
    assert Severity("error") == Severity.ERROR
    assert Severity("warning") == Severity.WARNING


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def test_veya_error_factory():
    err = veya_error(
        code="CUSTOM_CODE",
        message="custom message",
        severity="critical",
        component="custom",
        context={"x": 1},
    )
    assert isinstance(err, HicodeError)
    assert err.code == "CUSTOM_CODE"
    assert err.severity == Severity.CRITICAL
    assert err.component == "custom"
    assert err.context == {"x": 1}


# ---------------------------------------------------------------------------
# Concrete examples
# ---------------------------------------------------------------------------


def test_coordinator_timeout():
    err = CoordinatorTimeoutError("Task timed out", context={"task_id": "abc"})
    assert err.code == "COORDINATOR_TIMEOUT"
    assert err.component == "coordinator"
    assert err.context["task_id"] == "abc"


def test_permission_denied():
    err = PermissionDeniedError("write not allowed")
    assert err.code == "PERMISSION_DENIED"
    assert err.severity == Severity.WARNING


def test_sandbox_resource_exceeded():
    err = SandboxResourceExceeded(
        "Memory limit exceeded",
        context={"limit_mb": 512, "used_mb": 1024},
    )
    assert err.code == "SANDBOX_RESOURCE_EXCEEDED"
    assert err.severity == Severity.ERROR
    assert err.component == "sandbox"


def test_repr():
    err = HicodeError("fail", code="X", severity=Severity.CRITICAL, component="test")
    repr_str = repr(err)
    assert "HicodeError" in repr_str
    assert "'X'" in repr_str
    assert "'critical'" in repr_str


# ---------------------------------------------------------------------------
# Catch as Exception / BaseException
# ---------------------------------------------------------------------------


def test_can_be_caught_as_exception():
    with pytest.raises(HicodeError):
        raise ToolExecutionError("boom")

    with pytest.raises(Exception):  # noqa: B017 — verifying base-class catchability
        raise ConfigLoadError("missing file")
