"""C3 Step 1 — Canonical Execution ABI (contract only, no behaviour change).

This module defines the single execution contract that every GoalRun physical
execution must satisfy, regardless of whether it runs through an admitted
executor/harness or through a native substrate such as ``local2``.

Scope of Step 1 (deliberately narrow):

* define the frozen request/result contract and its references;
* reuse existing authorities — no new status, binding, receipt, activity,
  permission or persistence authority is introduced here;
* provide read-only contract validators;
* migrate **no** caller.

Design invariants encoded by the contract:

* ``admitted_executor`` is always the admitted executor (ExecutorRegistry
  authority) and is never overwritten by the substrate.
* ``substrate`` must equal the authoritative binding's
  ``effective_substrate_id``.
* ``bot_id`` must own the referenced ``goal_run_id`` (fail-closed).
* ``execution_id`` refers to the same real execution as the GoalRun; an adapter
  must never mint an unrelated id.
* the result status is the existing :class:`GoalStatus`; the ABI never decides
  completion on its own.
* activity and receipt are *references* to durable evidence, not copies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from runtime.bot_scope import require_same_bot
from server.goal_run.execution_admission import ExecutorBinding
from server.goal_run.models import GoalRunState, GoalStatus

__all__ = [
    "ActivityReference",
    "CanonicalExecutionPlane",
    "CanonicalGoalExecutionRequest",
    "CanonicalGoalExecutionResult",
    "ExecutionAdapter",
    "ExecutionAdapterError",
    "ExecutionBindingRef",
    "ExecutionContractError",
    "ExecutionPlaneMode",
    "ExecutionReceiptReference",
    "ExecutorHarnessAdapter",
    "HarnessEvidenceReference",
    "HarnessEvidenceSource",
    "HarnessExecutionAdapter",
    "HarnessRunOutcome",
    "Local2SubstrateAdapter",
    "substrate_for",
    "validate_binding_ref",
    "validate_harness_evidence",
    "validate_request",
    "validate_result",
    "validate_result_evidence",
]

_RECEIPTS_FILE = "execution_receipts.jsonl"


class ExecutionContractError(ValueError):
    """A canonical execution request/result violated the execution contract."""


@dataclass(frozen=True)
class ExecutionBindingRef:
    """Immutable reference to an authoritative ``ExecutorBinding`` record.

    Carries identifiers only; it is never a second source of mutable binding
    state. ``executor_id`` is the admitted executor and ``substrate_id`` is the
    physical substrate (see ``ExecutorBinding.effective_substrate_id``).
    """

    binding_id: str
    admission_id: str
    executor_id: str
    substrate_id: str = ""

    @property
    def effective_substrate_id(self) -> str:
        return self.substrate_id or self.executor_id

    @classmethod
    def from_binding(cls, binding: ExecutorBinding) -> ExecutionBindingRef:
        return cls(
            binding_id=binding.binding_id,
            admission_id=binding.admission_id,
            executor_id=binding.executor_id,
            substrate_id=binding.effective_substrate_id,
        )

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ExecutionBindingRef:
        """Build a reference from a persisted binding record (no mutable state)."""
        executor_id = str(record.get("executor_id") or "")
        return cls(
            binding_id=str(record.get("binding_id") or ""),
            admission_id=str(record.get("admission_id") or ""),
            executor_id=executor_id,
            substrate_id=str(record.get("substrate_id") or executor_id),
        )


@dataclass(frozen=True)
class ActivityReference:
    """Reference to one durable activity event (sequence is the authority)."""

    goal_run_id: str
    sequence: int
    activity_id: str = ""


@dataclass(frozen=True)
class ExecutionReceiptReference:
    """Reference to a durable execution receipt, never a constructed copy."""

    goal_run_id: str
    admission_id: str
    binding_id: str
    execution_id: str
    final_state: str = ""


class ExecutionPlaneMode(StrEnum):
    """The two physical execution strategies behind the canonical ABI."""

    substrate = "substrate"
    harness = "harness"


class HarnessEvidenceSource(StrEnum):
    """Which authoritative task-level record a harness reference resolves to."""

    task_delegate_result = "task.delegate_result"
    trust_plane_verified_state = "trust_plane.verified_state"


@dataclass(frozen=True)
class HarnessEvidenceReference:
    """Typed reference to task-level durable evidence from a harness execution.

    Names the authoritative GoalRun, the exact task, the evidence source and a
    durable locator, plus the artifact references. The reference is only a
    pointer: the validator resolves it against the persisted GoalRun/task record
    and refuses anything that is missing, empty, mismatched or unpersisted. A
    taskgraph file existing on disk is NOT by itself execution evidence. This is
    a reference, never a completion decision.
    """

    goal_run_id: str
    task_id: str
    source: str
    evidence_locator: str = ""
    artifact_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class HarnessRunOutcome:
    """What a harness adapter produced, for the plane to persist and reference.

    ``leaf_result`` is the real result of the existing ``execute_leaf`` path;
    the plane projects it into the authoritative GoalRun task record before
    building the evidence reference. It is never treated as proof on its own.
    """

    source: str
    leaf_result: Any = None
    artifacts: tuple[str, ...] = ()


@dataclass(frozen=True)
class CanonicalGoalExecutionRequest:
    """What should happen, expressed against the canonical execution plane.

    ``mode`` selects the execution strategy:

    * ``substrate`` must carry the authoritative durable ``binding``;
    * ``harness`` must NOT carry one (it runs through the existing
      HarnessRegistry contract and has no admission/binding grant).

    ``bot_id`` may be ``None`` to mean "no bot identity supplied". That is
    distinct from an explicit ``DEFAULT_BOT_ID`` owner and is refused.
    """

    goal_run_id: str
    task_id: str
    bot_id: str | None
    admitted_executor: str
    substrate: str
    objective: str
    binding: ExecutionBindingRef | None = None
    mode: ExecutionPlaneMode = ExecutionPlaneMode.substrate


@dataclass(frozen=True)
class CanonicalGoalExecutionResult:
    """How it physically happened, as references to durable evidence.

    ``status`` is the existing :class:`GoalStatus`. The ABI never owns
    completion: finalization still reads the GoalRun's canonical terminal state.

    Substrate executions carry ``activity`` + ``receipt``. Harness executions
    carry ``harness_evidence`` and must not fabricate a binding/receipt/activity.
    """

    execution_id: str
    status: GoalStatus
    admitted_executor: str
    substrate: str
    activity: ActivityReference | None = None
    receipt: ExecutionReceiptReference | None = None
    harness_evidence: HarnessEvidenceReference | None = None


def substrate_for(executor_id: str, substrate_id: str = "") -> str:
    """The existing default rule: an unset substrate falls back to the executor."""
    return substrate_id or executor_id


def _harness_execution_id(request: CanonicalGoalExecutionRequest) -> str:
    """A stable CORRELATION id derived from the authoritative identity.

    Harness executions have no authoritative persisted ExecutionControl run, so
    this id is a correlation identifier only: it names the same
    (GoalRun, task, harness) execution the evidence reference points at. It is
    never proof of execution or completion and does not represent a receipt.
    """
    return f"harness:{request.goal_run_id}:{request.task_id}"


def validate_request(request: CanonicalGoalExecutionRequest, *, state: GoalRunState) -> None:
    """Fail closed if the request is not consistent with canonical authority."""
    if request.goal_run_id != state.goal_id:
        raise ExecutionContractError(
            f"request goal_run_id {request.goal_run_id!r} does not match state {state.goal_id!r}"
        )
    # Bot isolation. ``None`` is a MISSING identity, never DEFAULT_BOT_ID.
    if request.bot_id is None:
        raise ExecutionContractError(
            "request is missing a bot identity; pass the authoritative GoalRun owner"
        )
    require_same_bot(request.bot_id, state.bot_id, f"goal_run:{request.goal_run_id}")
    if not request.admitted_executor:
        raise ExecutionContractError("admitted_executor must be a non-empty admitted executor")
    if not request.substrate:
        raise ExecutionContractError("substrate must be a non-empty physical substrate")

    if request.mode is ExecutionPlaneMode.harness:
        if request.binding is not None:
            raise ExecutionContractError(
                "harness requests must not carry an ExecutorBinding; a harness "
                "execution has no admission/binding grant"
            )
        return
    if request.mode is not ExecutionPlaneMode.substrate:
        raise ExecutionContractError(f"unknown execution mode {request.mode!r}")

    # Substrate mode requires the authoritative durable binding.
    if request.binding is None:
        raise ExecutionContractError(
            "substrate requests require the authoritative durable ExecutorBinding"
        )
    if state.requested_executor and request.admitted_executor != state.requested_executor:
        raise ExecutionContractError(
            f"admitted_executor {request.admitted_executor!r} is not the admitted "
            f"executor {state.requested_executor!r} for this run"
        )
    if request.binding.executor_id != request.admitted_executor:
        raise ExecutionContractError(
            "binding executor_id must equal the admitted_executor; a substrate "
            "must never overwrite the admitted executor identity"
        )
    if request.substrate != request.binding.effective_substrate_id:
        raise ExecutionContractError(
            f"substrate {request.substrate!r} does not match the authoritative "
            f"binding substrate {request.binding.effective_substrate_id!r}"
        )


def validate_result(
    result: CanonicalGoalExecutionResult, *, request: CanonicalGoalExecutionRequest
) -> None:
    """Fail closed if a result contradicts its request or fabricates identity."""
    if not isinstance(result.status, GoalStatus):
        raise ExecutionContractError("result status must be the canonical GoalStatus")
    if result.admitted_executor != request.admitted_executor:
        raise ExecutionContractError("result admitted_executor drifted from the request")
    if result.substrate != request.substrate:
        raise ExecutionContractError("result substrate drifted from the request")

    if request.mode is ExecutionPlaneMode.harness:
        if result.activity is not None or result.receipt is not None:
            raise ExecutionContractError(
                "harness result must not carry substrate activity/receipt evidence"
            )
        if result.harness_evidence is None:
            raise ExecutionContractError("harness result must reference harness evidence")
        if result.execution_id != _harness_execution_id(request):
            raise ExecutionContractError(
                "harness result execution_id must be derived from the authoritative request"
            )
        return

    # Substrate mode: the receipt/activity references are required.
    if not result.execution_id:
        raise ExecutionContractError("result must carry the real execution_id")
    if result.activity is None or result.receipt is None:
        raise ExecutionContractError("substrate result requires activity and receipt references")
    if result.activity.goal_run_id != request.goal_run_id:
        raise ExecutionContractError("activity reference points at a different GoalRun")
    if result.receipt.goal_run_id != request.goal_run_id:
        raise ExecutionContractError("receipt reference points at a different GoalRun")
    binding = request.binding
    if binding is not None:
        if binding.binding_id and result.receipt.binding_id != binding.binding_id:
            raise ExecutionContractError("receipt reference points at a different binding")
        if binding.admission_id and result.receipt.admission_id != binding.admission_id:
            raise ExecutionContractError("receipt reference points at a different admission")


def validate_binding_ref(ref: ExecutionBindingRef, binding_record: dict[str, Any]) -> None:
    """Fail closed if the request's binding ref contradicts the durable record."""
    record_executor = str(binding_record.get("executor_id") or "")
    record_substrate = str(binding_record.get("substrate_id") or record_executor)
    record_admission = str(binding_record.get("admission_id") or "")
    record_binding = str(binding_record.get("binding_id") or ref.binding_id)
    if ref.binding_id != record_binding:
        raise ExecutionContractError("binding ref does not match the durable binding id")
    if record_admission and ref.admission_id != record_admission:
        raise ExecutionContractError("binding ref admission_id does not match the durable binding")
    if ref.executor_id != record_executor:
        raise ExecutionContractError(
            "binding ref executor_id does not match the durable admitted executor"
        )
    if ref.effective_substrate_id != record_substrate:
        raise ExecutionContractError(
            "binding ref substrate does not match the durable binding substrate"
        )


def validate_harness_evidence(
    result: CanonicalGoalExecutionResult,
    *,
    request: CanonicalGoalExecutionRequest,
    project_root: str,
) -> None:
    """Resolve a harness reference against the AUTHORITATIVE persisted record.

    The reference is never trusted as supplied: this loads the GoalRun from disk
    and proves the evidence is task-scoped, attributed and durable. A taskgraph
    file existing on disk is not execution evidence on its own.
    """
    from server.goal_run.store import load_goal_run

    ref = result.harness_evidence
    if ref is None:
        raise ExecutionContractError("harness result carries no evidence reference")
    if ref.goal_run_id != request.goal_run_id:
        raise ExecutionContractError("harness evidence points at a different GoalRun")
    if ref.task_id != request.task_id:
        raise ExecutionContractError("harness evidence points at a different task")
    if not ref.source:
        raise ExecutionContractError("harness evidence must name its source")

    persisted = load_goal_run(project_root, request.goal_run_id)
    if persisted is None:
        raise ExecutionContractError("harness evidence GoalRun is not persisted")
    task = persisted.tasks.get(ref.task_id)
    if task is None:
        raise ExecutionContractError(
            "harness evidence task is not in the authoritative persisted GoalRun"
        )

    if ref.source == HarnessEvidenceSource.task_delegate_result:
        _validate_delegate_result_evidence(ref, task)
    elif ref.source == HarnessEvidenceSource.trust_plane_verified_state:
        _validate_trust_plane_evidence(ref, project_root)
    else:
        raise ExecutionContractError(f"unknown harness evidence source {ref.source!r}")


def _validate_delegate_result_evidence(ref: HarnessEvidenceReference, task: Any) -> None:
    delegate = task.delegate_result
    if not isinstance(delegate, dict) or not delegate:
        raise ExecutionContractError(
            "persisted task has no non-empty delegate_result; the taskgraph file "
            "existing is not execution evidence"
        )
    if str(delegate.get("delegate_id") or "") != ref.task_id:
        raise ExecutionContractError("delegate_result.delegate_id does not match the task")
    if str(delegate.get("goal_run_id") or "") != ref.goal_run_id:
        raise ExecutionContractError("delegate_result.goal_run_id does not match the GoalRun")

    persisted_artifacts = {str(a) for a in (task.artifacts or [])}
    referenced = {str(a) for a in ref.artifact_refs}
    if referenced != persisted_artifacts:
        raise ExecutionContractError(
            "harness artifact references do not match the authoritative persisted task artifact set"
        )
    for artifact in referenced:
        if not Path(artifact).exists():
            raise ExecutionContractError(
                f"harness artifact reference does not resolve to a durable file: {artifact!r}"
            )
    if ref.evidence_locator and not Path(ref.evidence_locator).exists():
        raise ExecutionContractError(
            f"harness evidence locator does not resolve: {ref.evidence_locator!r}"
        )


def _validate_trust_plane_evidence(ref: HarnessEvidenceReference, project_root: str) -> None:
    from server.goal_run.trust_plane import read_trust_plane_records

    records = read_trust_plane_records(project_root, ref.goal_run_id)
    claims = [
        r
        for r in records
        if r.get("_type") == "Claim"
        and str(r.get("task_id") or "") == ref.task_id
        and str(r.get("goal_id") or "") == ref.goal_run_id
    ]
    if not claims:
        raise ExecutionContractError("trust-plane evidence has no Claim for this GoalRun/task")
    claim_ids = {str(c.get("claim_id") or "") for c in claims}
    verified = [
        r
        for r in records
        if r.get("_type") == "VerifiedState"
        and str(r.get("task_id") or "") == ref.task_id
        and str(r.get("goal_id") or "") == ref.goal_run_id
        and str(r.get("claim_id") or "") in claim_ids
    ]
    if not verified:
        raise ExecutionContractError(
            "trust-plane evidence has no VerifiedState for this GoalRun/task"
        )


def validate_result_evidence(result: CanonicalGoalExecutionResult, *, project_root: str) -> None:
    """Read-only proof that a substrate result references durable evidence."""
    if result.receipt is None or result.activity is None:
        raise ExecutionContractError("substrate result requires activity and receipt references")
    receipt_path = (
        Path(project_root)
        / ".veya-project"
        / "goal-runs"
        / result.receipt.goal_run_id
        / _RECEIPTS_FILE
    )
    if not receipt_path.exists():
        raise ExecutionContractError("receipt reference has no durable receipt file")
    receipts = _read_jsonl(receipt_path)
    matched = any(
        str(record.get("binding_id") or "") == result.receipt.binding_id
        and str(record.get("provider_run_id") or "") == result.execution_id
        for record in receipts
    )
    if not matched:
        raise ExecutionContractError("receipt reference does not match any durable receipt")

    # Imported lazily to keep this module free of recorder construction side effects.
    from server.goal_run.execution_activity import ActivityRecorder

    activities, _ = ActivityRecorder(project_root).stream(result.activity.goal_run_id, cursor=0)
    if not any(a.sequence == result.activity.sequence for a in activities):
        raise ExecutionContractError("activity reference does not match any durable activity")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


# ── C3 Step 2/4b: canonical execution plane + adapters ──────────────────
#
# The plane only validates, selects an adapter, and reads back durable evidence.
# It owns no status, permission, registry, receipt/activity storage, or
# completion decision. Substrate adapters converge on ``ExecutionControl``; the
# harness adapter reuses the existing ``HarnessRegistry`` contract and never
# routes through ``ExecutionControl``.


class ExecutionAdapterError(RuntimeError):
    """No registered adapter could serve the authoritative binding."""


@runtime_checkable
class ExecutionAdapter(Protocol):
    """A physical execution strategy for one canonical request."""

    name: str

    def supports(self, binding_record: dict[str, Any]) -> bool: ...

    async def run(
        self,
        request: CanonicalGoalExecutionRequest,
        *,
        state: GoalRunState,
        project_root: str,
        binding_record: dict[str, Any],
        timeout_s: float = 600.0,
    ) -> HarnessRunOutcome | None: ...


_NATIVE_SUBSTRATE_KIND = "native_substrate"
_WORKER_SUBSTRATE_KINDS = frozenset({"cli_worker", "l1_worker", ""})


class Local2SubstrateAdapter:
    """Native-substrate path: Local2 is a substrate, never an admitted executor."""

    name = "local2_native"

    def supports(self, binding_record: dict[str, Any]) -> bool:
        return str(binding_record.get("substrate_kind") or "") == _NATIVE_SUBSTRATE_KIND

    async def run(
        self,
        request: CanonicalGoalExecutionRequest,
        *,
        state: GoalRunState,
        project_root: str,
        binding_record: dict[str, Any],
        timeout_s: float = 600.0,
    ) -> HarnessRunOutcome | None:
        if str(binding_record.get("substrate_kind") or "") != _NATIVE_SUBSTRATE_KIND:
            raise ExecutionContractError(
                "local2 adapter requires a native_substrate binding; refusing a "
                "non-native substrate without fallback"
            )
        # admitted_executor stays the admitted executor (e.g. codex); only the
        # substrate is local2. Local2 is never promoted to an admitted executor.
        await _run_via_execution_control(
            request, project_root=project_root, substrate=request.substrate, timeout_s=timeout_s
        )
        return None


class ExecutorHarnessAdapter:
    """Admitted-executor substrate path: validate admission, run the substrate."""

    name = "executor_harness"

    def supports(self, binding_record: dict[str, Any]) -> bool:
        kind = str(binding_record.get("substrate_kind") or "")
        return kind in _WORKER_SUBSTRATE_KINDS

    async def run(
        self,
        request: CanonicalGoalExecutionRequest,
        *,
        state: GoalRunState,
        project_root: str,
        binding_record: dict[str, Any],
        timeout_s: float = 600.0,
    ) -> HarnessRunOutcome | None:
        _require_admitted_executor(request.admitted_executor)
        if str(binding_record.get("executor_id") or "") != request.admitted_executor:
            raise ExecutionContractError(
                "executor/harness adapter requires admitted_executor to match the "
                "authoritative binding executor_id"
            )
        await _run_via_execution_control(
            request, project_root=project_root, substrate=request.substrate, timeout_s=timeout_s
        )
        return None


class HarnessExecutionAdapter:
    """Harness path: reuse the existing HarnessRegistry execution contract.

    This is selected explicitly by ``ExecutionPlaneMode.harness``, never by
    substrate kind. ``execute_leaf`` remains the SOLE owner of admission and
    harness authorization: the adapter does not pre-run those checks, it just
    drives the existing path and turns an explicit blocked/refused leaf into a
    fail-closed contract error. It does not route through ``ExecutionControl``
    and does not persist any receipt.
    """

    name = "harness"

    def supports(self, binding_record: dict[str, Any]) -> bool:
        return False  # mode-selected only; never by substrate_kind

    async def run(
        self,
        request: CanonicalGoalExecutionRequest,
        *,
        state: GoalRunState,
        project_root: str,
        binding_record: dict[str, Any],
        timeout_s: float = 600.0,
    ) -> HarnessRunOutcome | None:
        if request.mode is not ExecutionPlaneMode.harness:
            raise ExecutionContractError("harness adapter requires harness execution mode")
        if request.substrate != request.admitted_executor:
            raise ExecutionContractError(
                "harness execution binds the admitted executor and the harness to "
                "the same id; a mismatch would dispatch a different harness"
            )

        from server.goal_run.leaf import execute_leaf_with_memory

        # Authorization is NOT repeated here: ``execute_leaf`` owns admission and
        # harness registration. A refused leaf is fail-closed, never success.
        leaf_result = await execute_leaf_with_memory(
            project_root=project_root,
            instruction=request.objective,
            assignee=request.substrate,
        )
        if leaf_result.status != "completed":
            raise ExecutionContractError(
                f"harness leaf did not complete: {leaf_result.block_reason or leaf_result.status}"
            )
        artifacts = tuple(
            str(
                getattr(item, "path", None)
                or (item.get("path") if isinstance(item, dict) else item)
            )
            for item in (leaf_result.artifacts or [])
        )
        return HarnessRunOutcome(
            source=str(HarnessEvidenceSource.task_delegate_result),
            leaf_result=leaf_result,
            artifacts=artifacts,
        )


def _require_admitted_executor(admitted_executor: str) -> None:
    """ExecutorRegistry is the only admission authority; fail closed."""
    from veya.remote.executor_registry import get_executor_registry

    if admitted_executor not in get_executor_registry().snapshot():
        raise ExecutionContractError(
            f"executor not admitted by ExecutorRegistry: {admitted_executor!r}"
        )


async def _run_via_execution_control(
    request: CanonicalGoalExecutionRequest,
    *,
    project_root: str,
    substrate: str,
    timeout_s: float = 600.0,
) -> None:
    """Run one objective through the shared execution-control authority.

    No success is fabricated here: the adapter only drives the existing chain;
    the GoalRun terminal status and durable evidence remain authoritative.
    """
    from server.goal_run.execution_control import ExecutionControl

    await ExecutionControl(project_root).run_task(
        request.goal_run_id,
        command=request.objective,
        executor_id=substrate,
        timeout_s=timeout_s,
    )


class CanonicalExecutionPlane:
    """Single execution ABI: validate -> select adapter -> run -> canonical result."""

    def __init__(self, *, adapters: list[ExecutionAdapter] | None = None) -> None:
        self._adapters: list[ExecutionAdapter] = (
            list(adapters)
            if adapters is not None
            else [
                ExecutorHarnessAdapter(),
                Local2SubstrateAdapter(),
                HarnessExecutionAdapter(),
            ]
        )

    def adapter_for(self, binding_record: dict[str, Any]) -> ExecutionAdapter:
        for adapter in self._adapters:
            if adapter.supports(binding_record):
                return adapter
        raise ExecutionAdapterError(
            "no canonical execution adapter registered for binding substrate_kind "
            f"{binding_record.get('substrate_kind')!r}; refusing to fall back"
        )

    def _harness_adapter(self) -> ExecutionAdapter:
        for adapter in self._adapters:
            if adapter.name == "harness":
                return adapter
        raise ExecutionAdapterError(
            "no harness execution adapter registered; refusing to fall back"
        )

    async def execute(
        self,
        request: CanonicalGoalExecutionRequest,
        *,
        project_root: str,
        state: GoalRunState | None = None,
        timeout_s: float = 600.0,
    ) -> CanonicalGoalExecutionResult:
        from server.goal_run.store import load_goal_run

        if state is None:
            state = load_goal_run(project_root, request.goal_run_id)
        if state is None:
            raise ExecutionContractError(f"GoalRun {request.goal_run_id!r} not found")

        # 1. Validate identity against canonical authority.
        validate_request(request, state=state)

        if request.mode is ExecutionPlaneMode.harness:
            return await self._execute_harness(
                request, state=state, project_root=project_root, timeout_s=timeout_s
            )
        return await self._execute_substrate(
            request, state=state, project_root=project_root, timeout_s=timeout_s
        )

    async def _execute_substrate(
        self,
        request: CanonicalGoalExecutionRequest,
        *,
        state: GoalRunState,
        project_root: str,
        timeout_s: float,
    ) -> CanonicalGoalExecutionResult:
        from server.goal_run.execution_activity import ActivityRecorder
        from server.goal_run.execution_evidence import (
            finalize_execution_receipt,
            latest_receipt,
            load_binding_record,
        )
        from server.goal_run.store import load_goal_run

        binding = request.binding
        if binding is None:  # validate_request already guarantees this
            raise ExecutionContractError("substrate request is missing its binding")

        binding_record = load_binding_record(project_root, binding.binding_id)
        if binding_record is None:
            raise ExecutionContractError(
                f"no durable binding {binding.binding_id!r} for this execution"
            )
        validate_binding_ref(binding, binding_record)

        adapter = self.adapter_for(binding_record)
        await adapter.run(
            request,
            state=state,
            project_root=project_root,
            binding_record=binding_record,
            timeout_s=timeout_s,
        )

        final_state = load_goal_run(project_root, request.goal_run_id)
        if final_state is None:
            raise ExecutionContractError(
                f"GoalRun {request.goal_run_id!r} disappeared during execution"
            )
        finalize_execution_receipt(
            project_root=project_root,
            goal_run_id=request.goal_run_id,
            execution_id=str(final_state.execution_id or ""),
            admitted_executor=request.admitted_executor,
            substrate_id=request.substrate,
            final_state=final_state.status.value,
        )
        execution_id = str(final_state.execution_id or "")
        receipt_record = latest_receipt(
            project_root,
            request.goal_run_id,
            binding_id=binding.binding_id,
            execution_id=execution_id,
        )
        if receipt_record is None:
            raise ExecutionContractError("execution produced no durable receipt")

        activities, _ = ActivityRecorder(project_root).stream(request.goal_run_id, cursor=0)
        if not activities:
            raise ExecutionContractError("execution produced no durable activity")
        latest = activities[-1]

        result = CanonicalGoalExecutionResult(
            execution_id=execution_id,
            status=final_state.status,
            admitted_executor=request.admitted_executor,
            substrate=request.substrate,
            activity=ActivityReference(
                goal_run_id=request.goal_run_id,
                sequence=latest.sequence,
                activity_id=latest.activity_id,
            ),
            receipt=ExecutionReceiptReference(
                goal_run_id=request.goal_run_id,
                admission_id=str(receipt_record.get("admission_id") or ""),
                binding_id=str(receipt_record.get("binding_id") or ""),
                execution_id=str(receipt_record.get("provider_run_id") or ""),
                final_state=str(receipt_record.get("final_state") or ""),
            ),
        )
        validate_result(result, request=request)
        validate_result_evidence(result, project_root=project_root)
        return result

    async def _execute_harness(
        self,
        request: CanonicalGoalExecutionRequest,
        *,
        state: GoalRunState,
        project_root: str,
        timeout_s: float,
    ) -> CanonicalGoalExecutionResult:
        from server.goal_run.execution_evidence import persist_harness_task_evidence
        from server.goal_run.store import load_goal_run

        adapter = self._harness_adapter()
        outcome = await adapter.run(
            request,
            state=state,
            project_root=project_root,
            binding_record={},
            timeout_s=timeout_s,
        )
        if outcome is None or outcome.leaf_result is None:
            raise ExecutionContractError("harness adapter produced no execution outcome")

        # Persist the real leaf projection into the authoritative GoalRun task
        # record (existing delegate_result authority + store). The reference is
        # then built from -- and validated against -- that persisted record.
        persist_harness_task_evidence(
            project_root=project_root,
            goal_run_id=request.goal_run_id,
            task_id=request.task_id,
            objective=request.objective,
            leaf_result=outcome.leaf_result,
        )

        final_state = load_goal_run(project_root, request.goal_run_id)
        if final_state is None:
            raise ExecutionContractError(
                f"GoalRun {request.goal_run_id!r} disappeared during execution"
            )
        persisted_task = final_state.tasks.get(request.task_id)
        if persisted_task is None:
            raise ExecutionContractError(
                f"task {request.task_id!r} disappeared from the authoritative GoalRun"
            )

        taskgraph_path = (
            Path(project_root)
            / ".veya-project"
            / "goal-runs"
            / request.goal_run_id
            / "taskgraph.json"
        )
        result = CanonicalGoalExecutionResult(
            execution_id=_harness_execution_id(request),
            status=final_state.status,
            admitted_executor=request.admitted_executor,
            substrate=request.substrate,
            harness_evidence=HarnessEvidenceReference(
                goal_run_id=request.goal_run_id,
                task_id=request.task_id,
                source=outcome.source,
                evidence_locator=str(taskgraph_path) if taskgraph_path.exists() else "",
                artifact_refs=tuple(str(a) for a in persisted_task.artifacts),
            ),
        )
        validate_result(result, request=request)
        validate_harness_evidence(result, request=request, project_root=project_root)
        return result
