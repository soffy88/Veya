"""ExecutionAdmission + ExecutorBinding — P1 execution resource authority.

Absorbs the Codex Bridge admission/binding control-plane semantics without
introducing a second TaskManager, PermissionEngine, WorkspaceGate, or durable
GoalRun authority:

- GoalRun remains the only durable execution authority.
- PermissionEngine remains the only permission authority.
- ExecutorRegistry remains the only executor authority; this module only
  consumes its projections (identity/availability/selectable).
- Local2 remains the substrate/tool authority.

This module owns:

- ExecutionAdmission: whether a GoalRun may start executing, whether an
  executor may be selected, whether a workspace may be occupied, and which
  resources are leased.
- ExecutorBinding: the real executor runtime identity (provider session !=
  provider run), linked to a GoalRun and an admission.
- Receipts for admission/binding decisions, written into the GoalRun's own
  event log via the existing store helpers.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from server.goal_run.models import GoalStatus
from server.goal_run.store import append_event, load_goal_run, save_goal_run

logger = logging.getLogger("veya.goal_run.admission")

_ADMISSION_DIR = Path(".veya") / "execution_admission"
_ADMISSION_LOCK = threading.RLock()

#: GoalRun statuses that are terminal for resource ownership. A lease whose
#: owner run reached one of these is an orphan and must be reclaimed.
_TERMINAL_GOAL_STATUSES = frozenset(
    {
        GoalStatus.completed,
        GoalStatus.recovered,
        GoalStatus.partial_completed,
        GoalStatus.failed,
        GoalStatus.cancelled,
    }
)


class MutationIntent(StrEnum):
    READ_ONLY = "read_only"
    MUTATION = "mutation"
    EXCLUSIVE = "exclusive"


class ResourceMode(StrEnum):
    """P6: how a resource may be shared between concurrent executions."""

    SHARED = "SHARED"
    EXCLUSIVE = "EXCLUSIVE"


#: P6: canonical resource kinds. Order is the acquisition order and is
#: load-bearing: acquiring in a fixed global order is what makes multi-resource
#: admission deadlock-free (A holds R1 waits R2 / B holds R2 waits R1 cannot
#: both progress only if each acquires in a different order).
_RESOURCE_ORDER: tuple[str, ...] = (
    "project",
    "workspace",
    "executor-installation",
    "provider-session",
    "gpu",
    "browser-session",
    "desktop-session",
)

#: Kinds that are never shared, regardless of intent. ``executor-installation``
#: is deliberately NOT here: concurrent read-only use of the same installation
#: is allowed, only mutation is exclusive (handled by ``_intent_conflicts``).
_EXCLUSIVE_RESOURCE_KINDS = frozenset(
    {"provider-session", "gpu", "browser-session", "desktop-session"}
)


class AdmissionStatus(StrEnum):
    ADMITTED = "admitted"
    QUEUED = "queued"
    #: P6 canonical name for a resource wait. It is an alias of QUEUED so the
    #: durable status value stays backward-compatible.
    WAITING_FOR_RESOURCE = "queued"
    DENIED = "denied"
    EXPIRED = "expired"
    RELEASED = "released"
    REVOKED = "revoked"
    RECOVERY_REQUIRED = "recovery_required"


class BindingStatus(StrEnum):
    CREATED = "created"
    BOUND = "bound"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    LOST = "lost"
    RELEASED = "released"


_TERMINAL_BINDING = {
    BindingStatus.COMPLETED,
    BindingStatus.FAILED,
    BindingStatus.LOST,
    BindingStatus.RELEASED,
}


@dataclass(frozen=True)
class ResourceKey:
    kind: str  # project | workspace | executor-installation
    value: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.kind}:{self.value}"


@dataclass
class ResourceLease:
    lease_id: str
    resource: ResourceKey
    goal_run_id: str
    admission_id: str
    exclusive: bool
    acquired_at: str
    released_at: str | None = None
    intent: MutationIntent = MutationIntent.MUTATION
    workspace_root: str = ""

    def active(self) -> bool:
        return self.released_at is None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["intent"] = str(self.intent)
        return data


@dataclass(frozen=True)
class ProjectIdentity:
    project_root: str
    canonical_root: str
    git_common_dir: str | None

    def matches(self, other: ProjectIdentity) -> bool:
        return (
            self.canonical_root == other.canonical_root
            and self.git_common_dir == other.git_common_dir
        )


@dataclass(frozen=True)
class WorkspaceIdentity:
    workspace_root: str
    canonical_root: str
    git_toplevel: str | None
    git_common_dir: str | None

    def matches(self, other: WorkspaceIdentity) -> bool:
        return (
            self.canonical_root == other.canonical_root
            and self.git_common_dir == other.git_common_dir
        )


def _git(cwd: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def resolve_project_identity(project_root: str) -> ProjectIdentity:
    canonical = os.path.realpath(project_root)
    common = _git(canonical, "rev-parse", "--git-common-dir")
    if common is not None:
        common = os.path.realpath(os.path.join(canonical, common))
    return ProjectIdentity(
        project_root=project_root, canonical_root=canonical, git_common_dir=common
    )


def resolve_workspace_identity(workspace_root: str) -> WorkspaceIdentity:
    canonical = os.path.realpath(workspace_root)
    toplevel = _git(canonical, "rev-parse", "--show-toplevel")
    if toplevel is not None:
        toplevel = os.path.realpath(toplevel)
    common = _git(canonical, "rev-parse", "--git-common-dir")
    if common is not None:
        common = os.path.realpath(os.path.join(canonical, common))
    return WorkspaceIdentity(
        workspace_root=workspace_root,
        canonical_root=canonical,
        git_toplevel=toplevel,
        git_common_dir=common,
    )


@dataclass
class ExecutionAdmissionRequest:
    goal_run_id: str
    executor_id: str
    executor_installation_id: str
    project_id: str
    workspace_id: str
    project_root: str
    workspace_root: str
    requested_capabilities: list[str] = field(default_factory=list)
    mutation_intent: MutationIntent = MutationIntent.MUTATION
    network_access: bool = False
    required_resources: list[str] = field(default_factory=list)
    concurrency_class: str = "default"
    queue_if_busy: bool = False
    lease_ttl_s: float = 3600.0
    workspace_isolated: bool = False  # independent worktree => isolated writes


def _request_to_dict(req: ExecutionAdmissionRequest) -> dict[str, Any]:
    """Serialize an admission request for durable queue/recovery replay."""
    return {
        "goal_run_id": req.goal_run_id,
        "executor_id": req.executor_id,
        "executor_installation_id": req.executor_installation_id,
        "project_id": req.project_id,
        "workspace_id": req.workspace_id,
        "project_root": req.project_root,
        "workspace_root": req.workspace_root,
        "requested_capabilities": list(req.requested_capabilities),
        "mutation_intent": str(req.mutation_intent),
        "network_access": req.network_access,
        "required_resources": list(req.required_resources),
        "concurrency_class": req.concurrency_class,
        "queue_if_busy": req.queue_if_busy,
        "lease_ttl_s": req.lease_ttl_s,
        "workspace_isolated": req.workspace_isolated,
    }


def _request_from_dict(
    data: dict[str, Any], record: dict[str, Any] | None = None
) -> ExecutionAdmissionRequest:
    """Rebuild an admission request from a persisted record.

    Tolerates older records that stored only a partial request by falling back
    to the record's top-level identity fields.
    """
    record = record or {}
    try:
        intent = MutationIntent(str(data.get("mutation_intent") or "mutation"))
    except ValueError:
        intent = MutationIntent.MUTATION
    return ExecutionAdmissionRequest(
        goal_run_id=str(data.get("goal_run_id") or record.get("goal_run_id") or ""),
        executor_id=str(data.get("executor_id") or record.get("executor_id") or ""),
        executor_installation_id=str(data.get("executor_installation_id") or "default"),
        project_id=str(data.get("project_id") or data.get("project_root") or ""),
        workspace_id=str(data.get("workspace_id") or data.get("workspace_root") or ""),
        project_root=str(data.get("project_root") or ""),
        workspace_root=str(data.get("workspace_root") or ""),
        requested_capabilities=[str(c) for c in data.get("requested_capabilities") or []],
        mutation_intent=intent,
        network_access=bool(data.get("network_access", False)),
        required_resources=[str(r) for r in data.get("required_resources") or []],
        concurrency_class=str(data.get("concurrency_class") or "default"),
        queue_if_busy=bool(data.get("queue_if_busy", False)),
        lease_ttl_s=float(data.get("lease_ttl_s") or 3600.0),
        workspace_isolated=bool(data.get("workspace_isolated", False)),
    )


def resource_mode_for(kind: str, intent: MutationIntent) -> ResourceMode:
    """P6: how ``kind`` may be shared for the given intent.

    Read-only project/workspace access is shared; mutation is exclusive.
    Fixed-exclusive kinds (gpu/browser-session/...) are always exclusive.
    """
    normalized = str(kind).strip().lower().replace("_", "-")
    if normalized in _EXCLUSIVE_RESOURCE_KINDS:
        return ResourceMode.EXCLUSIVE
    if intent == MutationIntent.READ_ONLY:
        return ResourceMode.SHARED
    return ResourceMode.EXCLUSIVE


@dataclass
class ExecutionAdmissionResult:
    admission_id: str
    status: AdmissionStatus
    goal_run_id: str
    executor_id: str
    resource_leases: list[ResourceLease] = field(default_factory=list)
    expires_at: str | None = None
    reason: str = ""
    policy_revision: str = "p1"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def admitted(self) -> bool:
        return self.status == AdmissionStatus.ADMITTED

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = str(self.status)
        return data


@dataclass
class ExecutorBinding:
    binding_id: str
    executor_id: str
    installation_id: str
    provider_id: str
    provider_session_id: str | None
    provider_run_id: str | None
    goal_run_id: str
    admission_id: str
    capability_snapshot: list[str] = field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    status: BindingStatus = BindingStatus.CREATED
    provider_status: str | None = None
    provider_health: str | None = None
    approval_id: str | None = None
    approval_evidence: dict[str, Any] | None = None
    # C2.1 — TWO ROLES, FORMALLY MODELLED.
    # ``executor_id`` is the ADMITTED executor (ExecutorRegistry authority:
    # codex / pi / opencode / claude_code / grok / dsh / acp / antigravity).
    # ``substrate_id`` is the PHYSICAL substrate the work actually runs on
    # (adapter registry: local2 native_substrate, or a CLI worker wrapping an
    # executor). The two are different semantic layers and must never be
    # conflated in evidence. Empty means "same as executor_id" (the common
    # case where an executor runs through its own worker).
    substrate_id: str = ""
    executor_kind: str = ""
    substrate_kind: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def transition(self, status: BindingStatus) -> None:
        self.status = status
        self.updated_at = datetime.now(UTC).isoformat()

    def lost(self) -> bool:
        return self.status == BindingStatus.LOST

    @property
    def effective_substrate_id(self) -> str:
        """The substrate this binding actually runs on."""
        return self.substrate_id or self.executor_id

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = str(self.status)
        data["substrate_id"] = self.effective_substrate_id
        return data


@dataclass
class ExecutionReceipt:
    goal_run_id: str
    admission_id: str
    binding_id: str | None
    executor_id: str
    installation_id: str
    provider_session_id: str | None
    provider_run_id: str | None
    workspace_id: str
    permission_decision: str
    resource_leases: list[str]
    start_time: str
    end_time: str | None
    final_state: str
    verification_result: str | None = None
    # C2.1 — the receipt names BOTH roles explicitly, so evidence never has to
    # guess whether an id was the admitted executor or the physical substrate.
    substrate_id: str = ""
    executor_kind: str = ""
    substrate_kind: str = ""

    @property
    def effective_substrate_id(self) -> str:
        return self.substrate_id or self.executor_id

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["substrate_id"] = self.effective_substrate_id
        return data


def _admission_dir(project_root: str) -> Path:
    return Path(project_root) / _ADMISSION_DIR


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _load_admission_records(project_root: str) -> list[dict[str, Any]]:
    root = _admission_dir(project_root)
    if not root.is_dir():
        return []
    records = []
    for path in sorted(root.glob("admission_*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return records


def _load_binding_records(project_root: str) -> list[dict[str, Any]]:
    root = _admission_dir(project_root)
    if not root.is_dir():
        return []
    records = []
    for path in sorted(root.glob("binding_*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return records


# ---------------- ExecutionScope (LOCAL2_AUTH_REFORM) ----------------


DEFAULT_NORMAL_CAPABILITIES: tuple[str, ...] = (
    "file.read",
    "file.search",
    "file.write",
    "file.patch",
    "file.delete",
    "shell.exec",
    "test.run",
    "build.run",
    "git.read",
    "git.write",
    "worktree",
    "systemd.user",
    "network.project",
)


@dataclass
class ExecutionScope:
    goal_run_id: str
    project_id: str
    workspace_id: str
    workspace_root: str
    repository_root: str
    allowed_capabilities: list[str]
    mutation_intent: str
    network_policy: str
    executor_binding: str | None
    admission_id: str
    expires_at: str | None

    def allows(self, capability: str) -> bool:
        return capability in self.allowed_capabilities

    def covers(self, target: str) -> bool:
        try:
            real = os.path.realpath(target)
            root = os.path.realpath(self.workspace_root)
        except OSError:
            return False
        return real == root or real.startswith(root + os.sep)

    def expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            return datetime.now(UTC) > datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SCOPES: dict[str, ExecutionScope] = {}
_SCOPES_LOCK = threading.RLock()


def register_scope(scope: ExecutionScope, *, project_root: str | None = None) -> None:
    with _SCOPES_LOCK:
        _SCOPES[os.path.realpath(scope.workspace_root)] = scope
    if project_root is not None:
        try:
            _write_json(
                _admission_dir(project_root) / f"scope_{scope.admission_id}.json",
                scope.to_dict(),
            )
        except OSError:
            logger.warning("failed to persist execution scope %s", scope.admission_id)


def scope_for(target: str) -> ExecutionScope | None:
    try:
        real = os.path.realpath(target)
    except OSError:
        return None
    with _SCOPES_LOCK:
        candidates = [
            scope for scope in _SCOPES.values() if not scope.expired() and scope.covers(real)
        ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: len(os.path.realpath(s.workspace_root)))


def unregister_scope(admission_id: str) -> None:
    with _SCOPES_LOCK:
        for key in [k for k, s in _SCOPES.items() if s.admission_id == admission_id]:
            del _SCOPES[key]


def capability_for_tool(name: str, args: dict[str, Any] | None = None) -> str | None:
    args = args or {}
    if name == "shell.exec":
        command = args.get("command")
        if isinstance(command, str) and "systemctl" in command and "--user" in command:
            return "systemd.user"
        return "shell.exec"
    if name in (
        "file.read",
        "file.search",
        "file.write",
        "file.patch",
        "file.delete",
        "test.run",
        "build.run",
    ):
        return name
    if name in ("file.mkdir", "file.move"):
        return "file.write"
    if name in ("process.status", "process.cancel"):
        return "shell.exec"
    if name in ("build",):
        return "build.run"
    if name in ("git.status", "git.diff", "git.log"):
        return "git.read"
    if name in ("git.stage", "git.commit", "git.verify", "git.promote"):
        return "git.write"
    if name.startswith("worktree."):
        return "worktree"
    return None


def reset_scopes() -> None:
    with _SCOPES_LOCK:
        _SCOPES.clear()


def restore_scopes(project_root: str) -> int:
    """Rebuild in-memory scopes from persisted ADMITTED admission records."""
    restored = 0
    root = _admission_dir(project_root)
    if not root.is_dir():
        return 0
    for path in sorted(root.glob("scope_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            scope = ExecutionScope(**data)
        except (OSError, ValueError, TypeError):
            continue
        if scope.expired():
            continue
        with _SCOPES_LOCK:
            _SCOPES[os.path.realpath(scope.workspace_root)] = scope
        restored += 1
    return restored


def clear_all_scopes() -> None:
    reset_scopes()


# ---------------- P2: executor validity ----------------


@dataclass
class ExecutorAdmissionVerdict:
    ok: bool
    executor_id: str
    availability: str | None = None
    selectable: bool = False
    matched_capabilities: list[str] = field(default_factory=list)
    missing_capabilities: list[str] = field(default_factory=list)
    reason: str = ""


def validate_executor_for_admission(
    executor_id: str, requested_capabilities: list[str] | None = None
) -> ExecutorAdmissionVerdict:
    """Single executor-validity check for admission.

    Consumes ExecutorRegistry projections only; never second-guesses
    PermissionEngine or workspace policy.
    """
    from veya.remote.executor_registry import get_executor_registry

    try:
        identity = get_executor_registry().identity(executor_id)
    except Exception:
        return ExecutorAdmissionVerdict(
            ok=False, executor_id=executor_id, reason="executor_unknown"
        )

    availability = str(identity.availability)
    if availability in {"UNAVAILABLE", "DISABLED", "LEGACY"}:
        return ExecutorAdmissionVerdict(
            ok=False,
            executor_id=executor_id,
            availability=availability,
            selectable=False,
            reason=f"executor_unhealthy:{availability}",
        )
    if not identity.selectable:
        return ExecutorAdmissionVerdict(
            ok=False,
            executor_id=executor_id,
            availability=availability,
            selectable=False,
            reason="executor_not_selectable",
        )

    available_caps = {c.lower() for c in identity.capability.capabilities} | {
        op.lower() for op in identity.capability.supported_operations
    }
    matched: list[str] = []
    missing: list[str] = []
    for cap in requested_capabilities or []:
        if cap.lower() in available_caps:
            matched.append(cap)
        else:
            missing.append(cap)
    if missing:
        return ExecutorAdmissionVerdict(
            ok=False,
            executor_id=executor_id,
            availability=availability,
            selectable=True,
            matched_capabilities=matched,
            missing_capabilities=missing,
            reason=f"executor_capability_mismatch:{','.join(missing)}",
        )
    return ExecutorAdmissionVerdict(
        ok=True,
        executor_id=executor_id,
        availability=availability,
        selectable=True,
        matched_capabilities=matched,
        reason="admitted",
    )


class ExecutionAdmission:
    """Single execution-resource admission authority per project root."""

    def __init__(self, project_root: str) -> None:
        self.project_root = os.path.realpath(project_root)
        self._leases: dict[str, ResourceLease] = {}  # lease_id -> lease
        self._admissions: dict[str, ExecutionAdmissionResult] = {}

    # ---------------- admission ----------------

    def _intent_conflicts(
        self,
        incoming: MutationIntent,
        held: MutationIntent,
        same_workspace: bool,
        workspace_isolated: bool,
    ) -> bool:
        if incoming == MutationIntent.EXCLUSIVE or held == MutationIntent.EXCLUSIVE:
            return True
        if incoming == MutationIntent.READ_ONLY and held == MutationIntent.READ_ONLY:
            return False
        cross = not same_workspace or workspace_isolated
        if incoming == MutationIntent.MUTATION and held == MutationIntent.MUTATION:
            return not cross
        # MUTATION vs READ_ONLY on the *same shared* workspace: writes can
        # invalidate a concurrent reader, so require isolation to overlap.
        return bool(same_workspace and not workspace_isolated)

    def request(
        self,
        req: ExecutionAdmissionRequest,
        *,
        registered_project_root: str | None = None,
        registered_workspace_root: str | None = None,
    ) -> ExecutionAdmissionResult:
        with _ADMISSION_LOCK:
            verdict = validate_executor_for_admission(req.executor_id, req.requested_capabilities)
            if not verdict.ok:
                result = ExecutionAdmissionResult(
                    admission_id=f"adm_{uuid.uuid4().hex[:12]}",
                    status=AdmissionStatus.DENIED,
                    goal_run_id=req.goal_run_id,
                    executor_id=req.executor_id,
                    reason=verdict.reason,
                )
                self._persist_result(req, result)
                append_event(
                    req.project_root,
                    req.goal_run_id,
                    {
                        "type": "execution_admission_denied",
                        "goal_id": req.goal_run_id,
                        "admission_id": result.admission_id,
                        "reason": verdict.reason,
                    },
                )
                return result

            project_identity = resolve_project_identity(req.project_root)
            workspace_identity = resolve_workspace_identity(req.workspace_root)

            if registered_project_root is not None:
                registered = resolve_project_identity(registered_project_root)
                if not registered.matches(project_identity):
                    return self._deny(req, "project_identity_mismatch")
            if registered_workspace_root is not None:
                registered_ws = resolve_workspace_identity(registered_workspace_root)
                if not registered_ws.matches(workspace_identity):
                    return self._deny(req, "workspace_identity_mismatch")

            conflict = self._find_conflict(req)
            if conflict is not None:
                status = AdmissionStatus.QUEUED if req.queue_if_busy else AdmissionStatus.DENIED
                result = ExecutionAdmissionResult(
                    admission_id=f"adm_{uuid.uuid4().hex[:12]}",
                    status=status,
                    goal_run_id=req.goal_run_id,
                    executor_id=req.executor_id,
                    reason=f"resource_conflict:{conflict.resource}",
                )
                self._persist_result(req, result)
                append_event(
                    req.project_root,
                    req.goal_run_id,
                    {
                        "type": (
                            "execution_admission_queued"
                            if status == AdmissionStatus.QUEUED
                            else "execution_admission_denied"
                        ),
                        "goal_id": req.goal_run_id,
                        "admission_id": result.admission_id,
                        "reason": result.reason,
                    },
                )
                return result

            return self._grant(req)

    def _find_conflict(self, req: ExecutionAdmissionRequest) -> ResourceLease | None:
        """Return the active lease that blocks ``req``, or None if resources are free.

        P6: resources are acquired in a fixed global order (``_RESOURCE_ORDER``)
        so multi-resource admission is deadlock-free.
        """
        keys = self._resource_keys(req)
        for lease in self._leases.values():
            if not lease.active():
                continue
            if lease.resource not in keys:
                continue
            if lease.resource.kind in _EXCLUSIVE_RESOURCE_KINDS:
                return lease
            held_intent = lease.intent
            if lease.resource.kind == "executor-installation":
                same_workspace = True
                isolated = False
            else:
                same_workspace = lease.workspace_root == os.path.realpath(req.workspace_root)
                isolated = req.workspace_isolated
            if self._intent_conflicts(
                req.mutation_intent,
                held_intent,
                same_workspace=same_workspace,
                workspace_isolated=isolated,
            ):
                return lease
        return None

    def _grant(
        self, req: ExecutionAdmissionRequest, *, admission_id: str | None = None
    ) -> ExecutionAdmissionResult:
        """Create the resource leases and persist an ADMITTED admission."""
        admission_id = admission_id or f"adm_{uuid.uuid4().hex[:12]}"
        keys = self._resource_keys(req)
        leases = []
        now = datetime.now(UTC).isoformat()
        for key in keys:
            lease = ResourceLease(
                lease_id=f"lease_{uuid.uuid4().hex[:12]}",
                resource=key,
                goal_run_id=req.goal_run_id,
                admission_id=admission_id,
                exclusive=req.mutation_intent == MutationIntent.EXCLUSIVE,
                acquired_at=now,
                intent=req.mutation_intent,
                workspace_root=os.path.realpath(req.workspace_root),
            )
            leases.append(lease)
            self._leases[lease.lease_id] = lease

        expires = (datetime.now(UTC) + timedelta(seconds=req.lease_ttl_s)).isoformat()
        result = ExecutionAdmissionResult(
            admission_id=admission_id,
            status=AdmissionStatus.ADMITTED,
            goal_run_id=req.goal_run_id,
            executor_id=req.executor_id,
            resource_leases=leases,
            expires_at=expires,
            reason="admitted",
        )
        self._admissions[admission_id] = result
        self._persist_result(req, result)
        append_event(
            req.project_root,
            req.goal_run_id,
            {
                "type": "execution_admission_admitted",
                "goal_id": req.goal_run_id,
                "admission_id": admission_id,
                "executor_id": req.executor_id,
                "resource_leases": [str(lease.resource) for lease in leases],
                "expires_at": expires,
            },
        )
        return result

    def admit_queued(self, project_root: str) -> list[ExecutionAdmissionResult]:
        """Drain queued admissions FIFO, admitting those whose resources are free.

        P6: the queue is durable state in the existing admission records — no
        second scheduler or queue store. A queued admission keeps its identity;
        only its status and leases change.
        """
        admitted: list[ExecutionAdmissionResult] = []
        with _ADMISSION_LOCK:
            records = [
                r
                for r in _load_admission_records(project_root)
                if str(r.get("status")) == str(AdmissionStatus.QUEUED)
            ]
            # FIFO: oldest created_at first.
            records.sort(key=lambda r: str(r.get("created_at") or ""))
            for rec in records:
                req = _request_from_dict(rec.get("request") or {}, rec)
                if self._find_conflict(req) is not None:
                    continue
                result = self._grant(req, admission_id=str(rec.get("admission_id")))
                admitted.append(result)
        return admitted

    def queued_admissions(self, project_root: str) -> list[dict[str, Any]]:
        """FIFO-ordered view of queued admissions (WAITING_FOR_RESOURCE)."""
        records = [
            r
            for r in _load_admission_records(project_root)
            if str(r.get("status")) == str(AdmissionStatus.QUEUED)
        ]
        records.sort(key=lambda r: str(r.get("created_at") or ""))
        return records

    def _resource_keys(self, req: ExecutionAdmissionRequest) -> list[ResourceKey]:
        keys = [
            ResourceKey("project", os.path.realpath(req.project_root)),
            ResourceKey("workspace", os.path.realpath(req.workspace_root)),
            ResourceKey(
                "executor-installation",
                f"{req.executor_id}/{req.executor_installation_id}",
            ),
        ]
        seen = {key.kind for key in keys}
        # P6: additional resources declared by the caller (gpu, browser-session,
        # provider-session, desktop-session, ...). "kind:value" or bare "kind".
        for raw in req.required_resources or []:
            text = str(raw).strip()
            if not text:
                continue
            if ":" in text:
                kind, value = text.split(":", 1)
            else:
                kind, value = text, "default"
            kind = kind.strip().lower().replace("_", "-")
            if not kind or kind in seen:
                continue
            seen.add(kind)
            keys.append(ResourceKey(kind, value.strip() or "default"))
        # Fixed global order: deadlock-free multi-resource acquisition.
        return sorted(
            keys,
            key=lambda k: (
                _RESOURCE_ORDER.index(k.kind)
                if k.kind in _RESOURCE_ORDER
                else len(_RESOURCE_ORDER),
                k.value,
            ),
        )

    def _deny(self, req: ExecutionAdmissionRequest, reason: str) -> ExecutionAdmissionResult:
        result = ExecutionAdmissionResult(
            admission_id=f"adm_{uuid.uuid4().hex[:12]}",
            status=AdmissionStatus.DENIED,
            goal_run_id=req.goal_run_id,
            executor_id=req.executor_id,
            reason=reason,
        )
        self._persist_result(req, result)
        append_event(
            req.project_root,
            req.goal_run_id,
            {
                "type": "execution_admission_denied",
                "goal_id": req.goal_run_id,
                "admission_id": result.admission_id,
                "reason": reason,
            },
        )
        return result

    def _persist_result(
        self, req: ExecutionAdmissionRequest, result: ExecutionAdmissionResult
    ) -> None:
        path = _admission_dir(req.project_root) / f"admission_{result.admission_id}.json"
        payload = result.to_dict()
        # P6: persist the *full* request so a queued admission can be admitted
        # later (FIFO drain) without a second queue store.
        payload["request"] = _request_to_dict(req)
        # P5: full lease records so a restart can restore active leases and
        # reclaim orphans without a second lease store.
        payload["leases"] = [lease.to_dict() for lease in result.resource_leases]
        try:
            _write_json(path, payload)
        except OSError:
            logger.warning("failed to persist admission %s", result.admission_id)

    # ---------------- P5: lease recovery ----------------

    def restore_leases(self, project_root: str) -> int:
        """Rebuild in-memory leases from persisted ADMITTED admissions.

        Only admissions whose GoalRun is still non-terminal are restored: a
        lease for a completed/cancelled/failed goal is an orphan and must not
        keep a workspace locked forever.
        """
        restored = 0
        with _ADMISSION_LOCK:
            for rec in _load_admission_records(project_root):
                if str(rec.get("status")) != str(AdmissionStatus.ADMITTED):
                    continue
                goal_run_id = str(rec.get("goal_run_id") or "")
                state = load_goal_run(project_root, goal_run_id) if goal_run_id else None
                if state is not None and state.status in _TERMINAL_GOAL_STATUSES:
                    continue
                for raw in rec.get("leases") or []:
                    if not isinstance(raw, dict) or raw.get("released_at"):
                        continue
                    try:
                        lease = ResourceLease(
                            lease_id=str(raw["lease_id"]),
                            resource=ResourceKey(
                                str(raw["resource"]["kind"]), str(raw["resource"]["value"])
                            ),
                            goal_run_id=str(raw.get("goal_run_id") or goal_run_id),
                            admission_id=str(raw.get("admission_id") or rec.get("admission_id")),
                            exclusive=bool(raw.get("exclusive", False)),
                            acquired_at=str(raw.get("acquired_at") or ""),
                            released_at=None,
                            intent=MutationIntent(str(raw.get("intent") or "mutation")),
                            workspace_root=str(raw.get("workspace_root") or ""),
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                    self._leases[lease.lease_id] = lease
                    restored += 1
        return restored

    def reclaim_orphan_leases(self, project_root: str) -> int:
        """Release persisted admissions whose GoalRun is terminal/missing.

        This is the counterpart of :meth:`restore_leases`: an orphan must not
        keep a workspace permanently locked after its owner run ended.
        """
        reclaimed = 0
        with _ADMISSION_LOCK:
            for rec in _load_admission_records(project_root):
                if str(rec.get("status")) != str(AdmissionStatus.ADMITTED):
                    continue
                goal_run_id = str(rec.get("goal_run_id") or "")
                state = load_goal_run(project_root, goal_run_id) if goal_run_id else None
                orphan = state is None or state.status in _TERMINAL_GOAL_STATUSES
                if not orphan:
                    continue
                for lease in self._leases.values():
                    if lease.admission_id == rec.get("admission_id") and lease.active():
                        lease.released_at = datetime.now(UTC).isoformat()
                rec["status"] = str(AdmissionStatus.RELEASED)
                with contextlib.suppress(OSError):
                    _write_json(
                        _admission_dir(project_root) / f"admission_{rec['admission_id']}.json",
                        rec,
                    )
                reclaimed += 1
        return reclaimed

    def release(self, admission_id: str, *, project_root: str | None = None) -> bool:
        with _ADMISSION_LOCK:
            released = False
            for lease in self._leases.values():
                if lease.admission_id == admission_id and lease.active():
                    lease.released_at = datetime.now(UTC).isoformat()
                    released = True
            if admission_id in self._admissions:
                self._admissions[admission_id].status = AdmissionStatus.RELEASED
            unregister_scope(admission_id)
            if project_root is not None:
                scope_path = _admission_dir(project_root) / f"scope_{admission_id}.json"
                with contextlib.suppress(OSError):
                    scope_path.unlink(missing_ok=True)
            if released and project_root is not None:
                for rec in _load_admission_records(project_root):
                    if rec.get("admission_id") == admission_id:
                        rec["status"] = str(AdmissionStatus.RELEASED)
                        with contextlib.suppress(OSError):
                            _write_json(
                                _admission_dir(project_root) / f"admission_{admission_id}.json",
                                rec,
                            )
            return released

    # ---------------- binding ----------------

    def create_binding(
        self,
        *,
        executor_id: str,
        installation_id: str,
        provider_id: str,
        goal_run_id: str,
        admission_id: str,
        provider_session_id: str | None = None,
        provider_run_id: str | None = None,
        capability_snapshot: list[str] | None = None,
        model: str | None = None,
        effort: str | None = None,
        substrate_id: str = "",
        executor_kind: str = "",
        substrate_kind: str = "",
        project_root: str,
    ) -> ExecutorBinding:
        # C2.1 — the binding records both roles. ``executor_id`` stays the
        # admitted executor; ``substrate_id`` is the physical substrate the
        # grant is allowed to run on (may differ for a native substrate).
        if not executor_kind:
            try:
                from veya.remote.executor_registry import get_executor_registry

                executor_kind = str(get_executor_registry().identity(executor_id).executor_kind)
            except Exception:
                executor_kind = ""
        if not substrate_kind:
            substrate_key = substrate_id or executor_id
            try:
                from veya.remote.executor_adapter import get_executor_adapter

                adapter = get_executor_adapter(substrate_key, project_root)
                substrate_kind = str(adapter.identity.executor_kind) if adapter else ""
            except Exception:
                substrate_kind = ""
        binding = ExecutorBinding(
            binding_id=f"bind_{uuid.uuid4().hex[:12]}",
            executor_id=executor_id,
            installation_id=installation_id,
            provider_id=provider_id,
            provider_session_id=provider_session_id,
            provider_run_id=provider_run_id,
            goal_run_id=goal_run_id,
            admission_id=admission_id,
            capability_snapshot=list(capability_snapshot or []),
            model=model,
            effort=effort,
            status=BindingStatus.CREATED,
            substrate_id=substrate_id,
            executor_kind=executor_kind,
            substrate_kind=substrate_kind,
        )
        self._persist_binding(project_root, binding)
        append_event(
            project_root,
            goal_run_id,
            {
                "type": "executor_binding_created",
                "goal_id": goal_run_id,
                "binding_id": binding.binding_id,
                "admission_id": admission_id,
                "executor_id": executor_id,
                "substrate_id": binding.effective_substrate_id,
            },
        )
        return binding

    def sync_provider_snapshot(
        self, project_root: str, binding: ExecutorBinding
    ) -> ExecutorBinding:
        """Refresh provider runtime identity onto the binding (registry is authority)."""
        from veya.remote.executor_registry import get_executor_registry

        try:
            identity = get_executor_registry().identity(binding.executor_id)
        except Exception:
            binding.provider_status = "UNKNOWN"
            binding.provider_health = "UNKNOWN"
        else:
            binding.provider_status = identity.status
            binding.provider_health = identity.health
        binding.updated_at = datetime.now(UTC).isoformat()
        self._persist_binding(project_root, binding)
        append_event(
            project_root,
            binding.goal_run_id,
            {
                "type": "executor_binding_provider_snapshot",
                "goal_id": binding.goal_run_id,
                "binding_id": binding.binding_id,
                "provider_status": binding.provider_status,
                "provider_health": binding.provider_health,
            },
        )
        return binding

    def record_approval_evidence(
        self,
        project_root: str,
        binding: ExecutorBinding,
        *,
        approval_id: str,
        decision: str,
        source: str = "permission_engine",
    ) -> ExecutorBinding:
        binding.approval_id = approval_id
        binding.approval_evidence = {
            "approval_id": approval_id,
            "decision": decision,
            "source": source,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        binding.updated_at = datetime.now(UTC).isoformat()
        self._persist_binding(project_root, binding)
        append_event(
            project_root,
            binding.goal_run_id,
            {
                "type": "executor_binding_approval",
                "goal_id": binding.goal_run_id,
                "binding_id": binding.binding_id,
                "approval_id": approval_id,
                "decision": decision,
            },
        )
        return binding

    def update_binding(
        self,
        project_root: str,
        binding: ExecutorBinding,
        *,
        status: BindingStatus | None = None,
        provider_session_id: str | None = None,
        provider_run_id: str | None = None,
    ) -> ExecutorBinding:
        if status is not None:
            binding.transition(status)
        if provider_session_id is not None:
            binding.provider_session_id = provider_session_id
        if provider_run_id is not None:
            binding.provider_run_id = provider_run_id
        binding.updated_at = datetime.now(UTC).isoformat()
        self._persist_binding(project_root, binding)
        return binding

    def _persist_binding(self, project_root: str, binding: ExecutorBinding) -> None:
        try:
            _write_json(
                _admission_dir(project_root) / f"binding_{binding.binding_id}.json",
                binding.to_dict(),
            )
        except OSError:
            logger.warning("failed to persist binding %s", binding.binding_id)

    # ---------------- recovery ----------------

    def reconcile_restart(self, project_root: str) -> list[dict[str, Any]]:
        """After a process restart, no live lease exists.

        Any persisted binding that is not terminal and whose provider
        evidence is missing must not be reported as running.
        """
        recovered: list[dict[str, Any]] = []
        with _ADMISSION_LOCK:
            for record in _load_binding_records(project_root):
                status = BindingStatus(record.get("status", "created"))
                if status in _TERMINAL_BINDING:
                    continue
                record["status"] = str(BindingStatus.LOST)
                record["updated_at"] = datetime.now(UTC).isoformat()
                unregister_scope(str(record.get("admission_id", "")))
                with contextlib.suppress(OSError):
                    _write_json(
                        _admission_dir(project_root) / f"binding_{record['binding_id']}.json",
                        record,
                    )
                state = load_goal_run(project_root, record.get("goal_run_id", ""))
                if state is not None and state.status in (
                    GoalStatus.running,
                    GoalStatus.recovering,
                ):
                    state.status = GoalStatus.recovering
                    state.last_stop_reason = "executor_binding_lost_recovery_required"
                    save_goal_run(state, project_root)
                    append_event(
                        project_root,
                        state.goal_id,
                        {
                            "type": "executor_binding_lost",
                            "goal_id": state.goal_id,
                            "binding_id": record.get("binding_id"),
                        },
                    )
                recovered.append(record)
        return recovered


def record_execution_receipt(project_root: str, receipt: ExecutionReceipt) -> None:
    path = (
        Path(project_root)
        / ".veya-project"
        / "goal-runs"
        / receipt.goal_run_id
        / "execution_receipts.jsonl"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt.to_dict(), sort_keys=True) + "\n")
    except OSError:
        logger.warning("failed to write execution receipt for %s", receipt.goal_run_id)


_INSTANCES: dict[str, ExecutionAdmission] = {}
_INSTANCES_LOCK = threading.RLock()


def get_admission(project_root: str) -> ExecutionAdmission:
    key = os.path.realpath(project_root)
    with _INSTANCES_LOCK:
        inst = _INSTANCES.get(key)
        if inst is None:
            inst = ExecutionAdmission(project_root)
            _INSTANCES[key] = inst
        return inst


def reset_admissions() -> None:
    with _INSTANCES_LOCK:
        _INSTANCES.clear()
