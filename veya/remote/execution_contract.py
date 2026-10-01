from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from veya.supervision.evidence import (
    GENESIS_HASH,
    build_evidence_chain,
    canonical_content_hash,
    verify_evidence_chain,
)

from .executor_registry import get_executor_registry, normalize_executor_id
from .worker_runtime import capabilities_for


@dataclass
class RuntimeCapabilityManifest:
    executor_id: str
    executor_kind: str
    installed: bool
    authenticated: bool
    reachable: bool

    provider: str
    model: str
    runtime_version: str

    supports_streaming: bool
    supports_cancel: bool
    supports_suspend: bool
    supports_resume: bool
    supports_session_reuse: bool
    supports_handoff: bool

    supports_workspace: bool
    supports_nested_repo: bool
    supports_worktree: bool

    supports_mcp: bool
    supports_skills: bool
    supports_shell: bool

    filesystem_isolation: str
    network_isolation: str
    credential_isolation: str
    process_isolation: str

    max_concurrency: int
    active_executions: int

    status: Literal["READY", "DEGRADED", "UNAVAILABLE", "UNKNOWN"]
    status_reason: str
    observed_at: float
    runtime_source: str = "unknown"
    launcher: str | None = None
    auth_state: str = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionCapabilityEnvelope:
    filesystem: dict[str, list[str]]
    network: dict[str, Any]
    tools: dict[str, list[str]]
    mcp_servers: dict[str, list[str]]
    skills: dict[str, list[str]]
    credentials: dict[str, list[str]]
    compute: dict[str, Any]
    workspace: Any
    runtime: Any

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionSpec:
    objective: str
    executor_requirements: dict[str, Any]
    workspace: Any
    capabilities: ExecutionCapabilityEnvelope
    resources: dict[str, Any]
    supervision: dict[str, Any]
    continuation: Any
    task_contract: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionCondition:
    type: str
    status: Literal["TRUE", "FALSE", "UNKNOWN"]
    reason: str
    message: str
    observed_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SessionEnvelope:
    mission_id: str
    execution_id: str
    executor_id: str
    session_id: str
    objective: str
    workspace: Any
    accepted_progress: Any
    evidence_refs: list[str]
    git_state: Any
    continuation_summary: str
    created_at: float
    generation: int = 1
    parent_execution_id: str | None = None
    forked_from_session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def handoff(
        self,
        target_executor_id: str,
        *,
        reason: str,
        continuation_summary: str | None = None,
        new_execution_id: str | None = None,
    ) -> SessionEnvelope:
        """Handoff session continuation to a target executor, incrementing generation."""
        import uuid

        norm_target = target_executor_id.lower().strip()
        manifest = probe_runtime_capability_manifest(norm_target, workspace_path=self.workspace)
        if manifest.status == "UNAVAILABLE":
            raise ValueError(
                f"Cannot handoff to executor '{target_executor_id}': status is UNAVAILABLE ({manifest.status_reason})"
            )
        summary = (
            continuation_summary or f"Handoff from {self.executor_id} to {norm_target}: {reason}"
        )
        exec_id = new_execution_id or f"ex_{uuid.uuid4().hex[:16]}"
        return SessionEnvelope(
            mission_id=self.mission_id,
            execution_id=exec_id,
            executor_id=norm_target,
            session_id=self.session_id,
            objective=self.objective,
            workspace=self.workspace,
            accepted_progress=self.accepted_progress,
            evidence_refs=list(self.evidence_refs),
            git_state=self.git_state,
            continuation_summary=summary,
            created_at=time.time(),
            generation=self.generation + 1,
            parent_execution_id=self.execution_id,
            forked_from_session_id=self.forked_from_session_id,
        )

    def fork(
        self,
        new_objective: str,
        *,
        branch_name: str | None = None,
        new_executor_id: str | None = None,
    ) -> SessionEnvelope:
        """Branch this session into an isolated child session."""
        import uuid

        fork_exec_id = f"ex_fork_{uuid.uuid4().hex[:12]}"
        fork_session_id = f"{self.session_id}_fork_{uuid.uuid4().hex[:8]}"
        git_state = dict(self.git_state) if isinstance(self.git_state, dict) else {}
        if branch_name:
            git_state["branch"] = branch_name
        return SessionEnvelope(
            mission_id=self.mission_id,
            execution_id=fork_exec_id,
            executor_id=new_executor_id or self.executor_id,
            session_id=fork_session_id,
            objective=new_objective,
            workspace=self.workspace,
            accepted_progress=self.accepted_progress,
            evidence_refs=list(self.evidence_refs),
            git_state=git_state,
            continuation_summary=f"Forked from execution {self.execution_id}",
            created_at=time.time(),
            generation=1,
            parent_execution_id=self.execution_id,
            forked_from_session_id=self.session_id,
        )


def probe_runtime_capability_manifest(
    executor_id: str,
    *,
    workspace_path: str | Any | None = None,
    health_registry: Any = None,
    active_executions: int = 0,
) -> RuntimeCapabilityManifest:
    """Actively probes the host environment and runtime profile for an executor.

    Verifies actual binary installation, credentials, health registry status,
    and workspace capabilities. Never returns static assumptions.
    """
    import shutil
    import subprocess
    import time
    from pathlib import Path

    norm = normalize_executor_id(executor_id)
    try:
        identity = get_executor_registry().identity(norm)
    except ValueError:
        # A capability probe answers "is this executor usable here?", so an
        # executor the registry does not admit is a legitimate answer — report it
        # as not installed rather than raising. identity() is a strict lookup now,
        # so this is the seam that keeps a probe from becoming an admission.
        return RuntimeCapabilityManifest(
            executor_id=norm,
            executor_kind="l1_worker",
            installed=False,
            authenticated=False,
            reachable=False,
            provider="unknown",
            model="unknown",
            runtime_version="unknown",
            supports_streaming=False,
            supports_cancel=False,
            supports_suspend=False,
            supports_resume=False,
            supports_session_reuse=False,
            supports_handoff=False,
            supports_workspace=False,
            supports_nested_repo=False,
            supports_worktree=False,
            supports_mcp=False,
            supports_skills=False,
            supports_shell=False,
            filesystem_isolation="none",
            network_isolation="none",
            credential_isolation="none",
            process_isolation="none",
            max_concurrency=0,
            active_executions=0,
            observed_at=time.time(),
            status="UNAVAILABLE",
            status_reason="not installed",
            launcher=None,
        )

    installed = False
    bin_path = None
    version = "unknown"
    authenticated = identity.authenticated
    reachable = identity.reachable
    provider = identity.provider or "unknown"
    model = identity.model or "unknown"

    # 1. Probe installation and version
    candidate_bins: list[str | Path] = []
    if norm == "antigravity":
        candidate_bins = [
            "antigravity",
            "agy",
            Path.home() / ".local" / "bin" / "agy",
            Path.home() / ".gemini" / "antigravity-cli" / "bin" / "agy",
        ]
    elif norm == "opencode":
        candidate_bins = [
            "opencode",
            Path.home() / ".opencode" / "bin" / "opencode",
            Path.home() / ".local" / "bin" / "opencode",
        ]
    elif norm == "claude_code":
        candidate_bins = [
            "claude",
            Path.home() / ".local" / "bin" / "claude",
        ]
    elif norm == "codex":
        candidate_bins = [
            "codex",
            Path.home() / ".local" / "bin" / "codex",
            Path.home() / ".nvm" / "versions" / "node" / "v26.4.0" / "bin" / "codex",
        ]
    elif norm == "pi":
        candidate_bins = [
            "pi",
            Path.home() / ".local" / "bin" / "pi",
            Path.home() / ".nvm" / "versions" / "node" / "v26.4.0" / "bin" / "pi",
        ]
    elif norm == "grok":
        candidate_bins = [
            "grok",
            Path.home() / ".grok" / "bin" / "grok",
            Path.home() / ".local" / "bin" / "grok",
        ]
    elif norm == "dsh":
        candidate_bins = [
            "dsh",
            Path.home() / ".local" / "bin" / "dsh",
        ]
    elif norm == "acp":
        from veya.remote.acp_adapter import resolve_acp_command

        resolved = resolve_acp_command()
        candidate_bins = [resolved[0]] if resolved else ["openhands", "acp-agent", "agents-cli"]
    elif norm == "hicode":
        raise ValueError("Executor retired: 'hicode'")

    if not installed:
        for cand in candidate_bins:
            found = shutil.which(str(cand))
            if found:
                installed = True
                bin_path = found
                break

    if installed and bin_path and version == "unknown":
        try:
            res = subprocess.run(
                [bin_path, "--version"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            out = (res.stdout or res.stderr).strip().splitlines()
            if out:
                version = out[0][:64]
        except Exception:
            version = "probed-unversioned"

    # 2. Probe health registry if present
    health_reason = ""
    if health_registry is not None:
        try:
            h = health_registry.get_health(norm)
            h_str = getattr(h, "value", str(h))
            if h_str == "UNAVAILABLE":
                reachable = False
                health_reason = "health registry: provider UNAVAILABLE"
            elif h_str == "DEGRADED":
                health_reason = "health registry: provider DEGRADED"
        except Exception as exc:
            reachable = False
            health_reason = f"health check failed: {exc}"

    # 3. Determine status and status_reason
    if not installed:
        status: Literal["READY", "DEGRADED", "UNAVAILABLE", "UNKNOWN"] = "UNAVAILABLE"
        status_reason = f"binary for {norm} not installed or not executable"
    elif not reachable:
        status = "UNAVAILABLE"
        status_reason = health_reason or f"provider for {norm} unreachable"
    elif not authenticated:
        status = "DEGRADED"
        status_reason = f"credentials for {norm} not configured"
    elif health_reason:
        status = "DEGRADED"
        status_reason = health_reason
    else:
        status = "READY"
        status_reason = f"active probe passed: binary at {bin_path} ({version})"

    # 4. Probe workspace capability
    supports_workspace = True
    supports_worktree = True
    supports_nested_repo = True
    if workspace_path:
        wp = Path(workspace_path)
        supports_workspace = wp.exists()
        supports_worktree = (wp / ".git").exists() or shutil.which("git") is not None

    return RuntimeCapabilityManifest(
        executor_id=norm,
        executor_kind="l1_worker",
        installed=installed,
        authenticated=authenticated,
        reachable=reachable,
        provider=provider,
        model=model,
        runtime_version=version,
        supports_streaming=True,
        supports_cancel=True,
        supports_suspend=True,
        supports_resume=True,
        supports_session_reuse=True,
        supports_handoff=False,
        supports_workspace=supports_workspace,
        supports_nested_repo=supports_nested_repo,
        supports_worktree=supports_worktree,
        supports_mcp=True,
        supports_skills=True,
        # Publish observed runtime capability, not a theoretical executor
        # promise. Dispatch consumes the same worker registry contract.
        supports_shell=capabilities_for(norm).supports_shell_effect,
        filesystem_isolation="isolated_worktree",
        network_isolation="loopback_proxy",
        credential_isolation="ephemeral_redacted",
        process_isolation="process_group",
        max_concurrency=4,
        active_executions=active_executions,
        status=status,
        status_reason=status_reason,
        observed_at=time.time(),
        runtime_source=identity.runtime_source,
        launcher=identity.launcher,
        auth_state=identity.auth_state,
    )


# The runtime capability probe shells out to `<binary> --version` with a 2s
# timeout, so running it inline inside worker.dispatch admission makes the
# admission request pay that latency once per child.  The probe is a pure
# function of (executor, workspace, health), so memoise it for a short window.
# Admission still AWAITS a real manifest -- it just stops re-probing binaries
# on every dispatch.  Health is part of the cache key, so a provider going
# UNAVAILABLE invalidates immediately instead of waiting out the TTL.
MANIFEST_CACHE_TTL_S = 15.0

_manifest_cache: dict[tuple[str, str, str], tuple[float, RuntimeCapabilityManifest]] = {}
_manifest_cache_lock = threading.Lock()


def _manifest_health_key(executor_id: str, health_registry: Any | None) -> str:
    if health_registry is None:
        return ""
    try:
        return str(getattr(health_registry.get_health(executor_id), "value", ""))
    except Exception:
        return "unknown"


def probe_runtime_capability_manifest_cached(
    executor_id: str,
    *,
    workspace_path: str | Any | None = None,
    health_registry: Any = None,
    active_executions: int = 0,
    ttl_s: float = MANIFEST_CACHE_TTL_S,
) -> RuntimeCapabilityManifest:
    """Memoised :func:`probe_runtime_capability_manifest` for the admission path.

    Callers must still await this; the cache removes repeat subprocess cost,
    it does not make the probe fire-and-forget.
    """
    norm = normalize_executor_id(executor_id)
    key = (norm, str(workspace_path or ""), _manifest_health_key(norm, health_registry))
    now = time.time()
    with _manifest_cache_lock:
        cached = _manifest_cache.get(key)
        if cached is not None and now - cached[0] < ttl_s:
            # active_executions/observed_at are live bookkeeping, not probe
            # results: refresh them so a cached manifest never misreports.
            return replace(cached[1], active_executions=active_executions, observed_at=now)

    manifest = probe_runtime_capability_manifest(
        norm,
        workspace_path=workspace_path,
        health_registry=health_registry,
        active_executions=active_executions,
    )
    with _manifest_cache_lock:
        _manifest_cache[key] = (now, manifest)
    return manifest


def clear_runtime_capability_manifest_cache() -> None:
    """Drop memoised manifests. Used by tests and by explicit re-qualification."""
    with _manifest_cache_lock:
        _manifest_cache.clear()


__all__ = [
    "GENESIS_HASH",
    "MANIFEST_CACHE_TTL_S",
    "ExecutionCapabilityEnvelope",
    "ExecutionCondition",
    "ExecutionSpec",
    "RuntimeCapabilityManifest",
    "SessionEnvelope",
    "build_evidence_chain",
    "canonical_content_hash",
    "clear_runtime_capability_manifest_cache",
    "probe_runtime_capability_manifest",
    "probe_runtime_capability_manifest_cached",
    "verify_evidence_chain",
]
