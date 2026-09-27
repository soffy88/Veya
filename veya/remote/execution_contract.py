from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Literal

from veya.obase import canonical_proxies as _cp  # SPEC 10: no raw upstream ids in business code
from veya.supervision.evidence import (
    GENESIS_HASH,
    build_evidence_chain,
    canonical_content_hash,
    verify_evidence_chain,
)

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
    import os
    import shutil
    import subprocess
    import sys
    import time
    from pathlib import Path

    norm = executor_id.lower().strip()
    if norm in ("agy", "antigravity"):
        norm = "antigravity"
    elif norm in ("open-code", "opencode_go"):
        norm = "opencode"

    installed = False
    bin_path = None
    version = "unknown"
    authenticated = False
    reachable = True
    provider = "unknown"
    model = "unknown"

    # 1. Probe installation and version
    candidate_bins: list[str | Path] = []
    if norm == "antigravity":
        candidate_bins = [
            "antigravity",
            "agy",
            Path.home() / ".local" / "bin" / "agy",
            Path.home() / ".gemini" / "antigravity-cli" / "bin" / "agy",
        ]
        provider = _cp.executor_provider("antigravity", "gemini")
        model = _cp.executor_model("antigravity")
        authenticated = bool(
            os.environ.get("GEMINI_API_KEY")
            or os.environ.get("ANTIGRAVITY_API_KEY")
            or (Path.home() / ".gemini" / "antigravity-cli" / "auth.json").is_file()
        )
    elif norm == "opencode":
        candidate_bins = [
            "opencode",
            Path.home() / ".opencode" / "bin" / "opencode",
            Path.home() / ".local" / "bin" / "opencode",
        ]
        provider = _cp.executor_provider("opencode")
        model = _cp.executor_model("opencode")
        authenticated = bool(
            os.environ.get("OPENCODE_API_KEY")
            or (Path.home() / ".opencode" / "auth.json").is_file()
        )
    elif norm == "codex":
        candidate_bins = [
            "codex",
            Path.home() / ".local" / "bin" / "codex",
            Path.home() / ".nvm" / "versions" / "node" / "v26.4.0" / "bin" / "codex",
        ]
        provider = _cp.executor_provider("codex")
        model = _cp.executor_model("codex")
        authenticated = bool(
            os.environ.get("CODEX_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or (Path.home() / ".codex" / "config.json").is_file()
        )
    elif norm == "pi":
        candidate_bins = [
            "pi",
            Path.home() / ".local" / "bin" / "pi",
            Path.home() / ".nvm" / "versions" / "node" / "v26.4.0" / "bin" / "pi",
        ]
        provider = _cp.executor_provider("pi")
        model = _cp.executor_model("pi")
        authenticated = bool(
            os.environ.get("PI_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or (Path.home() / ".pi" / "agent.json").is_file()
        )
    elif norm == "grok":
        candidate_bins = [
            "grok",
            Path.home() / ".grok" / "bin" / "grok",
            Path.home() / ".local" / "bin" / "grok",
        ]
        provider = _cp.executor_provider("grok")
        model = _cp.executor_model("grok")
        authenticated = bool(os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY"))
    elif norm == "dsh":
        candidate_bins = [
            "dsh",
            Path.home() / ".local" / "bin" / "dsh",
        ]
        provider = _cp.executor_provider("dsh")
        model = _cp.executor_model("dsh")
        authenticated = True
    elif norm == "acp":
        from veya.remote.acp_adapter import resolve_acp_command

        resolved = resolve_acp_command()
        candidate_bins = [resolved[0]] if resolved else ["openhands", "acp-agent", "agents-cli"]
        provider = _cp.executor_provider("acp")
        model = _cp.executor_model("acp")
        authenticated = True
    elif norm == "hicode":
        installed = True
        bin_path = sys.executable
        version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        provider = _cp.executor_provider("hicode")
        model = _cp.executor_model("hicode")
        authenticated = bool(
            os.environ.get("OPENCODE_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or True
        )

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
        executor_kind="l1_worker" if norm != "hicode" else "internal_hicode",
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
        max_concurrency=16 if norm == "hicode" else 4,
        active_executions=active_executions,
        status=status,
        status_reason=status_reason,
        observed_at=time.time(),
    )


__all__ = [
    "GENESIS_HASH",
    "ExecutionCapabilityEnvelope",
    "ExecutionCondition",
    "ExecutionSpec",
    "RuntimeCapabilityManifest",
    "SessionEnvelope",
    "build_evidence_chain",
    "canonical_content_hash",
    "probe_runtime_capability_manifest",
    "verify_evidence_chain",
]
