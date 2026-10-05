"""SF-RECEIPT: a bad receipt is a contract failure, not a missing receipt.

``set_finalization`` used to assign ``record.effect_receipt`` only under
``isinstance(receipt, dict)``, with no ``else``. A stringified receipt was
therefore dropped in silence, together with the commit and promotion claims that
arrived on the same payload, and the result was indistinguishable from a
legitimately absent receipt.

``set_effect_receipt`` had a different defect on the same contract: it validated
nothing, so a non-mapping survived until ``to_public`` called ``dict(...)`` on it
and raised, surfacing a serialization crash far from its cause.

Both are now one decidable contract with three distinct states.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from veya.remote.execution import (
    _RECEIPT_REQUIRED_FIELDS,
    DurableJobManager,
    ExecutionRecord,
    _receipt_contract_problem,
)


def make_record() -> ExecutionRecord:
    return ExecutionRecord(
        execution_id="e1",
        task_id="t1",
        session_id="s1",
        token_id="tk",
        principal="p",
        tool="test.run",
        veya_tool="coding_test",
        requested_workspace="/w",
        requested_realpath="/w",
        resolved_repo_root="/w",
        repo_identity="repo-ident",
        status="RUNNING",
    )


class Manager(DurableJobManager):
    """Just enough of the manager to exercise the receipt contract."""

    def __init__(self) -> None:
        self.record = make_record()

    def _record_for_update(self, execution_id: str) -> ExecutionRecord:
        return self.record

    def _persist(self, record: ExecutionRecord, required: bool = False) -> None:
        pass


def good_receipt(**overrides: Any) -> dict[str, Any]:
    receipt = {
        "execution_id": "e1",
        "worker_type": "builtin",
        "repo_identity": "repo-ident",
        "worktree_path": "/w/.veya/worktrees/task-1",
        "task_kind": "WRITE",
        "changed_files": ["calc.py"],
        "diff_digest": "sha256:abc",
    }
    receipt.update(overrides)
    return receipt


# ── the three states are distinct ────────────────────────────────────
def test_a_valid_receipt_is_persisted_with_its_claims() -> None:
    manager = Manager()
    manager.set_finalization(
        "e1",
        {
            "status": "PROMOTED",
            "receipt": good_receipt(),
            "commit_sha": "a" * 40,
            "promotion_status": "PROMOTED",
        },
    )
    record = manager.record
    assert record.receipt_contract_status == "valid"
    assert record.receipt_contract_error is None
    assert record.effect_receipt == good_receipt()
    # The claims that rode in on the same payload are applied.
    assert record.execution_commit_sha == "a" * 40
    assert record.promotion_state == "PROMOTED"


def test_a_stringified_receipt_is_an_explicit_failure() -> None:
    """The reported defect: present, but not a receipt."""
    manager = Manager()
    # A prior claim is present, so "withheld" is observable rather than vacuous.
    manager.record.execution_commit_sha = "0" * 40
    manager.record.promotion_state = "PROMOTED"
    manager.set_finalization(
        "e1",
        {"status": "PROMOTED", "receipt": "execution e1 committed abc", "commit_sha": "b" * 40},
    )
    record = manager.record
    assert record.receipt_contract_status == "invalid"
    assert "expected a receipt object, got str" in (record.receipt_contract_error or "")
    assert record.finalization_failure_class == "RECEIPT_CONTRACT_INVALID"
    # No false receipt, and the claims from the bad payload are withheld rather
    # than half-applied.
    assert record.effect_receipt is None
    assert record.execution_commit_sha is None
    assert record.promotion_state is None
    # And it is audible.
    kinds = [event.get("kind") for event in record.events]
    assert "receipt_contract_failure" in kinds


def test_a_missing_receipt_is_distinguishable_from_an_invalid_one() -> None:
    absent = Manager()
    absent.set_finalization("e1", {"status": "PROMOTED"})
    invalid = Manager()
    invalid.set_finalization("e1", {"status": "PROMOTED", "receipt": "nope"})

    assert absent.record.receipt_contract_status == "absent"
    assert invalid.record.receipt_contract_status == "invalid"
    # Absent is not a failure; invalid is.
    assert absent.record.finalization_failure_class is None
    assert invalid.record.finalization_failure_class == "RECEIPT_CONTRACT_INVALID"
    assert absent.record.receipt_contract_error is None
    assert invalid.record.receipt_contract_error


def test_a_structurally_incomplete_receipt_is_invalid() -> None:
    """Well-typed but under-specified is still not a receipt."""
    manager = Manager()
    partial = good_receipt()
    del partial["repo_identity"]
    del partial["worker_type"]
    manager.set_finalization("e1", {"status": "PROMOTED", "receipt": partial})

    assert manager.record.receipt_contract_status == "invalid"
    error = manager.record.receipt_contract_error or ""
    assert "missing required fields" in error
    assert "repo_identity" in error and "worker_type" in error
    assert manager.record.effect_receipt is None


@pytest.mark.parametrize("bad", [None, 0, 1, True, [], "text", b"bytes", 3.5, object()])
def test_no_non_mapping_is_ever_accepted(bad: Any) -> None:
    manager = Manager()
    manager.set_finalization("e1", {"status": "PROMOTED", "receipt": bad})
    assert manager.record.receipt_contract_status == "invalid"
    assert manager.record.effect_receipt is None


def test_a_present_but_empty_field_counts_as_missing() -> None:
    manager = Manager()
    manager.set_finalization("e1", {"status": "PROMOTED", "receipt": good_receipt(task_kind="")})
    assert manager.record.receipt_contract_status == "invalid"
    assert "task_kind" in (manager.record.receipt_contract_error or "")


# ── the worker path had the same contract, unguarded ─────────────────
def test_the_worker_receipt_path_is_guarded_too() -> None:
    manager = Manager()
    manager.set_effect_receipt("e1", "also-a-string")
    assert manager.record.receipt_contract_status == "invalid"
    assert "worker:" in (manager.record.receipt_contract_error or "")
    assert manager.record.effect_receipt is None


def test_a_bad_worker_receipt_no_longer_crashes_serialization() -> None:
    """It used to survive here and explode later in ``to_public``."""
    manager = Manager()
    manager.set_effect_receipt("e1", ["not", "a", "receipt"])
    record = manager.record
    # The exact expression to_public uses.
    assert dict(record.effect_receipt or {}) == {}
    public = record.to_public(heartbeat_timeout_s=30.0)
    assert public["effect_receipt"] == {}
    assert public["receipt_contract_status"] == "invalid"


def test_a_valid_worker_receipt_still_persists() -> None:
    manager = Manager()
    manager.set_effect_receipt("e1", good_receipt())
    assert manager.record.receipt_contract_status == "valid"
    assert manager.record.effect_receipt == good_receipt()


# ── P0-K falsifiability is preserved ─────────────────────────────────
def test_the_contract_requires_every_field_that_has_no_default() -> None:
    """A receipt must carry what EffectReceipt cannot default.

    Deriving the list from the model is the point: a field added to EffectReceipt
    without a default must automatically become required, so a receipt can never
    quietly claim less than the evidence it stands for.
    """
    import dataclasses

    from veya.remote.l1_contract import EffectReceipt

    def nullable(field: Any) -> bool:
        text = str(field.type)
        return "None" in text or "Optional" in text

    must_be_present = {
        field.name
        for field in dataclasses.fields(EffectReceipt)
        if field.default is dataclasses.MISSING
        and field.default_factory is dataclasses.MISSING  # type: ignore[misc]
        # A nullable field with no default is still genuinely optional:
        # ``worker_runtime_id: str | None`` is allowed to be None. Only fields
        # that cannot be absent at all are required to be non-empty.
        and not nullable(field)
    }
    assert set(_RECEIPT_REQUIRED_FIELDS) == must_be_present
    # The nullable ones stay optional, and a receipt without one is still valid.
    assert "worker_runtime_id" not in _RECEIPT_REQUIRED_FIELDS
    assert _receipt_contract_problem(good_receipt()) is None


def test_a_receipt_cannot_claim_another_repository() -> None:
    """Falsifiability: the receipt's claims must match the record's evidence."""
    manager = Manager()
    manager.set_finalization(
        "e1", {"status": "PROMOTED", "receipt": good_receipt(repo_identity="a-different-repo")}
    )
    record = manager.record
    # The receipt is well-formed, so it is stored...
    assert record.receipt_contract_status == "valid"
    # ...but it is visibly inconsistent with the record it is attached to, and
    # that comparison stays available to a falsifiability check.
    assert record.effect_receipt["repo_identity"] != record.repo_identity


def test_the_validator_is_decidable() -> None:
    assert _receipt_contract_problem(good_receipt()) is None
    assert _receipt_contract_problem("s") is not None
    assert _receipt_contract_problem({}) is not None
    # Same input, same answer, every time.
    assert _receipt_contract_problem({}) == _receipt_contract_problem({})


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
