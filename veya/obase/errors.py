"""
veya Error Taxonomy

Structured, machine-readable error hierarchy for all veya components.
Every exception inherits from HicodeError and carries:
  - code:        stable string identifier (e.g. "COORDINATOR_TIMEOUT")
  - severity:    one of "info" | "warning" | "error" | "critical"
  - component:   owning module name (e.g. "coordinator", "sandbox")
  - context:     optional dict with additional debugging data
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Severity levels
# ---------------------------------------------------------------------------


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Base exception
# ---------------------------------------------------------------------------


class HicodeError(Exception):
    """Root exception for all veya errors."""

    code: str = "VEYA_UNKNOWN"
    severity: Severity = Severity.ERROR
    component: str = "veya"

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        severity: Severity | str | None = None,
        component: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.severity = (
            Severity(severity) if isinstance(severity, str) else (severity or self.severity)
        )
        self.component = component or self.component
        self.context: dict[str, Any] = context or {}

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a machine-readable dict (for JSON API / logging)."""
        return {
            "error": True,
            "code": self.code,
            "severity": self.severity.value,
            "component": self.component,
            "message": self.message,
            "context": self.context,
        }

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"code={self.code!r}, "
            f"severity={self.severity.value!r}, "
            f"component={self.component!r}, "
            f"message={self.message!r})"
        )


# ---------------------------------------------------------------------------
# Sub-category mixins
# ---------------------------------------------------------------------------


class _CoordinatorMixin(HicodeError):
    component = "coordinator"


class _SandboxMixin(HicodeError):
    component = "sandbox"


class _RegistryMixin(HicodeError):
    component = "registry"


class _StreamMixin(HicodeError):
    component = "streaming"


class _ToolMixin(HicodeError):
    component = "tools"


class _ConfigMixin(HicodeError):
    component = "config"


class _SessionMixin(HicodeError):
    component = "session"


class _ModelMixin(HicodeError):
    component = "model"


class _HookMixin(HicodeError):
    component = "hooks"


class _PermissionMixin(HicodeError):
    component = "permission"
    severity = Severity.WARNING


# ---------------------------------------------------------------------------
# Concrete exceptions — Coordinator
# ---------------------------------------------------------------------------


class CoordinatorTimeoutError(_CoordinatorMixin):
    """Task exceeded its deadline."""

    code = "COORDINATOR_TIMEOUT"
    severity = Severity.ERROR


class CoordinatorDAGError(_CoordinatorMixin):
    """Cycle or unresolved dependency in task DAG."""

    code = "COORDINATOR_DAG_ERROR"
    severity = Severity.CRITICAL


class CheckpointError(_CoordinatorMixin):
    """Failed to save or restore a checkpoint."""

    code = "COORDINATOR_CHECKPOINT"
    severity = Severity.ERROR


# ---------------------------------------------------------------------------
# Concrete exceptions — Sandbox
# ---------------------------------------------------------------------------


class SandboxInitError(_SandboxMixin):
    """Sandbox could not be initialised."""

    code = "SANDBOX_INIT"
    severity = Severity.CRITICAL


class SandboxResourceExceeded(_SandboxMixin):
    """Execution exceeded memory / CPU / time limits."""

    code = "SANDBOX_RESOURCE_EXCEEDED"
    severity = Severity.ERROR


class SandboxFileAccessError(_SandboxMixin):
    """Attempted access outside sandbox boundary."""

    code = "SANDBOX_FILE_ACCESS"
    severity = Severity.WARNING


# ---------------------------------------------------------------------------
# Concrete exceptions — Registry
# ---------------------------------------------------------------------------


class RegistryNotFoundError(_RegistryMixin):
    """Requested tool / model / skill not found."""

    code = "REGISTRY_NOT_FOUND"
    severity = Severity.ERROR


class RegistryDuplicateError(_RegistryMixin):
    """Attempted to register a duplicate entry."""

    code = "REGISTRY_DUPLICATE"
    severity = Severity.WARNING


# ---------------------------------------------------------------------------
# Concrete exceptions — Streaming
# ---------------------------------------------------------------------------


class StreamConnectionError(_StreamMixin):
    """SSE / WebSocket connection lost."""

    code = "STREAM_CONNECTION"
    severity = Severity.ERROR


class StreamEncodingError(_StreamMixin):
    """Failed to encode / decode streamed token."""

    code = "STREAM_ENCODING"
    severity = Severity.ERROR


# ---------------------------------------------------------------------------
# Concrete exceptions — Tools
# ---------------------------------------------------------------------------


class ToolExecutionError(_ToolMixin):
    """Tool function raised an unexpected error."""

    code = "TOOL_EXECUTION"
    severity = Severity.ERROR


class ToolValidationError(_ToolMixin):
    """Tool input failed schema validation."""

    code = "TOOL_VALIDATION"
    severity = Severity.WARNING


class ToolNotAvailableError(_ToolMixin):
    """Tool is registered but currently unavailable (e.g. missing API key)."""

    code = "TOOL_UNAVAILABLE"
    severity = Severity.INFO


# ---------------------------------------------------------------------------
# Concrete exceptions — Config
# ---------------------------------------------------------------------------


class ConfigValidationError(_ConfigMixin):
    """Configuration failed schema / policy validation."""

    code = "CONFIG_VALIDATION"
    severity = Severity.CRITICAL


class ConfigLoadError(_ConfigMixin):
    """Configuration file could not be loaded."""

    code = "CONFIG_LOAD"
    severity = Severity.CRITICAL


# ---------------------------------------------------------------------------
# Concrete exceptions — Session
# ---------------------------------------------------------------------------


class SessionExpiredError(_SessionMixin):
    """Session TTL has elapsed."""

    code = "SESSION_EXPIRED"
    severity = Severity.INFO


class SessionStateError(_SessionMixin):
    """Inconsistent or corrupted session state."""

    code = "SESSION_STATE"
    severity = Severity.CRITICAL


# ---------------------------------------------------------------------------
# Concrete exceptions — Model
# ---------------------------------------------------------------------------


class ModelNotFoundError(_ModelMixin):
    """Requested LLM model / version not found."""

    code = "MODEL_NOT_FOUND"
    severity = Severity.ERROR


class ModelAPIError(_ModelMixin):
    """Upstream model provider returned an error."""

    code = "MODEL_API_ERROR"
    severity = Severity.ERROR


# ---------------------------------------------------------------------------
# Concrete exceptions — Hooks
# ---------------------------------------------------------------------------


class HookExecutionError(_HookMixin):
    """A lifecycle hook raised an exception."""

    code = "HOOK_EXECUTION"
    severity = Severity.ERROR


class HookAbortError(_HookMixin):
    """A hook explicitly aborted the pipeline."""

    code = "HOOK_ABORT"
    severity = Severity.WARNING


# ---------------------------------------------------------------------------
# Concrete exceptions — Permission
# ---------------------------------------------------------------------------


class PermissionDeniedError(_PermissionMixin):
    """Action blocked by permission policy."""

    code = "PERMISSION_DENIED"
    severity = Severity.WARNING


class PermissionScopeError(_PermissionMixin):
    """Requested scope is not recognised."""

    code = "PERMISSION_SCOPE"
    severity = Severity.ERROR


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


def veya_error(
    code: str,
    message: str = "",
    *,
    severity: Severity | str = Severity.ERROR,
    component: str = "veya",
    context: dict[str, Any] | None = None,
) -> HicodeError:
    """Create a HicodeError with arbitrary code / component at runtime."""
    return HicodeError(
        message,
        code=code,
        severity=severity,
        component=component,
        context=context,
    )
