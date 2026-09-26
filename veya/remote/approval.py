"""Server-issued single-use approval store (spec §29-§32, §38).

High-risk operations cannot rely on a client-supplied boolean ``approved=true``.
Only server-issued, strictly matched, single-use ApprovalRecords authorized by a
human UI can approve privileged actions.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from typing import Any

from .models import ApprovalRecord, RemoteErrorCode, RiskClass


def compute_operation_hash(normalized_operation: str, cwd: str, capability_id: str) -> str:
    """Return a deterministic SHA-256 hash of operation parameters."""
    norm_op = " ".join(normalized_operation.strip().split())
    norm_cwd = str(cwd or "").strip().rstrip("/")
    payload = f"{capability_id}:{norm_cwd}:{norm_op}".encode()
    return hashlib.sha256(payload).hexdigest()


class ApprovalStore:
    """In-memory thread-safe store for server-issued approvals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, ApprovalRecord] = {}

    def create_approval(
        self,
        *,
        principal: str,
        capability_id: str,
        normalized_operation: str,
        cwd: str,
        workspace: str,
        risk_class: RiskClass,
        ttl_s: float = 300.0,
        decision: str = "approved",
    ) -> ApprovalRecord:
        now = time.time()
        op_hash = compute_operation_hash(normalized_operation, cwd, capability_id)
        app_id = f"appr_{uuid.uuid4().hex[:16]}"
        record = ApprovalRecord(
            approval_id=app_id,
            principal=principal,
            capability_id=capability_id,
            normalized_operation=normalized_operation,
            operation_hash=op_hash,
            cwd=cwd,
            workspace=workspace,
            risk_class=risk_class,
            created_at=now,
            expires_at=now + max(0.001, float(ttl_s)),
            used_at=None,
            decision=decision,
        )
        with self._lock:
            self._records[app_id] = record
        return record

    def lookup(self, approval_id: str) -> ApprovalRecord | None:
        with self._lock:
            return self._records.get(approval_id)

    def decide(self, approval_id: str, *, principal: str, decision: str) -> ApprovalRecord:
        """Record the human decision without consuming the operation token."""
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        with self._lock:
            record = self._records.get(approval_id)
            if record is None:
                raise KeyError(approval_id)
            if record.principal != principal:
                raise PermissionError("approval principal mismatch")
            if record.used_at is not None:
                raise ValueError("approval has already been consumed")
            record.decision = decision
            return record

    def verify_and_consume(
        self,
        approval_id: str,
        *,
        principal: str,
        capability_id: str,
        normalized_operation: str,
        cwd: str,
        workspace: str,
        now: float | None = None,
    ) -> tuple[bool, RemoteErrorCode | None, str | None]:
        current_time = now if now is not None else time.time()
        with self._lock:
            record = self._records.get(approval_id)
            if record is None:
                return (
                    False,
                    RemoteErrorCode.INVALID_APPROVAL,
                    f"approval_id {approval_id!r} not found",
                )
            if record.decision != "approved":
                return (
                    False,
                    RemoteErrorCode.POLICY_BLOCKED,
                    f"approval {approval_id!r} has decision {record.decision!r}",
                )
            if record.used_at is not None:
                return (
                    False,
                    RemoteErrorCode.INVALID_APPROVAL,
                    f"approval {approval_id!r} has already been consumed (single-use)",
                )
            if current_time >= record.expires_at:
                return (
                    False,
                    RemoteErrorCode.APPROVAL_EXPIRED,
                    f"approval {approval_id!r} has expired",
                )
            if record.principal != principal:
                return (
                    False,
                    RemoteErrorCode.APPROVAL_MISMATCH,
                    f"approval principal mismatch: expected {record.principal!r}, got {principal!r}",
                )
            if record.capability_id != capability_id:
                return (
                    False,
                    RemoteErrorCode.APPROVAL_MISMATCH,
                    f"approval capability mismatch: expected {record.capability_id!r}, got {capability_id!r}",
                )
            expected_hash = compute_operation_hash(normalized_operation, cwd, capability_id)
            if record.operation_hash != expected_hash:
                return (
                    False,
                    RemoteErrorCode.APPROVAL_MISMATCH,
                    "approval operation mismatch: operation parameters do not match approved signature",
                )
            # Mark as consumed immediately (one-shot).
            record.used_at = current_time
            return True, None, None

    def list_records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in self._records.values()]

    def reset(self) -> None:
        with self._lock:
            self._records.clear()


_GLOBAL_APPROVAL_STORE: ApprovalStore | None = None
_STORE_LOCK = threading.Lock()


def get_approval_store() -> ApprovalStore:
    global _GLOBAL_APPROVAL_STORE
    if _GLOBAL_APPROVAL_STORE is None:
        with _STORE_LOCK:
            if _GLOBAL_APPROVAL_STORE is None:
                _GLOBAL_APPROVAL_STORE = ApprovalStore()
    return _GLOBAL_APPROVAL_STORE


def reset_approval_store() -> None:
    get_approval_store().reset()
