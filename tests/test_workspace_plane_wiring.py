"""P0-05 / P1-07 / P1-08: the workspace contract plane and action receipts.

Gates the production wiring, not the contract modules in isolation:

* ``runtime/coding/workspace_lifecycle.py`` records a canonical
  ``WorkspaceAuthority``/``WorkspaceRevision`` per workspace and promotes a
  ``WorkspaceProjection`` for the execution.
* A projection whose base revision the authority moved past is detected as
  stale and recorded as ``CONFLICT``/``STALE`` with a classified resolution
  strategy — never silently re-pointed at the newer revision.
* ``runtime/execution/side_effects.py`` emits one chained ``ActionReceipt``
  per high-impact declared side effect, verifiable with ``verify_chain``.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from pathlib import Path

import pytest

from runtime.coding.workspace_contracts import (
    AuthorityType,
    ConflictClass,
    ConflictStrategy,
    ProjectionState,
    WorkspaceAuthority,
    classify_conflict,
    is_stale,
    new_projection,
    new_revision,
    reconcile_projection,
    resolve_conflict,
)
from runtime.coding.workspace_lifecycle import (
    WorkspaceHandle,
    WorkspaceState,
    WorkspaceStore,
    _authority_type,
    attach_workspace,
    create_workspace,
    prepare_workspace,
    recover_workspace,
    release_workspace,
    reset_workspace,
    snapshot_workspace,
    workspace_authority,
    workspace_leases,
    workspace_projection,
    workspace_projection_conflict,
)
from runtime.execution.durable import (
    DurableExecutionRepository,
    WorkItemSpec,
    build_operation_key,
)
from runtime.execution.side_effects import SideEffectLedger
from server.action_receipt import ActionReceipt, verify_chain

DEAD_RUN = lambda run_id: False  # noqa: E731 - liveness evidence stub


@pytest.fixture()
def store(tmp_path: Path) -> WorkspaceStore:
    return WorkspaceStore(tmp_path / "veya")


@pytest.fixture()
def local_root(tmp_path: Path) -> Path:
    root = tmp_path / "ws-root"
    root.mkdir(parents=True, exist_ok=True)
    return root


# -- P0-05: canonical authority + revision --------------------------------------


def test_create_records_canonical_authority_and_revision(store: WorkspaceStore, local_root: Path):
    create_workspace(
        store, workspace_id="ws-1", owner_scope="task", root=str(local_root), kind="local"
    )

    authority = workspace_authority(store, "ws-1")
    assert authority is not None
    assert authority.workspace_id == "ws-1"
    assert authority.canonical_root == str(local_root)
    assert authority.authority_type is AuthorityType.FILESYSTEM
    revision = authority.current_revision
    assert revision.workspace_id == "ws-1"
    assert revision.revision_id
    assert revision.monotonic == 0


def test_recorded_authority_survives_restart(
    store: WorkspaceStore, local_root: Path, tmp_path: Path
):
    create_workspace(
        store, workspace_id="ws-1", owner_scope="task", root=str(local_root), kind="local"
    )
    before = workspace_authority(store, "ws-1")

    reopened = WorkspaceStore(tmp_path / "veya")
    after = workspace_authority(reopened, "ws-1")

    assert after == before
    assert after.current_revision.revision_id == before.current_revision.revision_id


def test_create_is_idempotent_and_keeps_one_revision(store: WorkspaceStore, local_root: Path):
    first = create_workspace(
        store, workspace_id="ws-1", owner_scope="task", root=str(local_root), kind="local"
    )
    again = create_workspace(
        store, workspace_id="ws-1", owner_scope="task", root=str(local_root), kind="local"
    )
    assert again == first
    assert workspace_authority(store, "ws-1").current_revision.monotonic == 0


def test_prepare_promotes_projection_on_current_revision(store: WorkspaceStore, local_root: Path):
    create_workspace(
        store, workspace_id="ws-1", owner_scope="task", root=str(local_root), kind="local"
    )
    attach_workspace(store, "ws-1", "run-a")
    prepared = prepare_workspace(store, "ws-1")

    authority = workspace_authority(store, "ws-1")
    projection = workspace_projection(store, "ws-1")
    assert projection is not None
    assert projection.base_revision == authority.current_revision
    assert projection.state is ProjectionState.ACTIVE
    assert projection.path == str(local_root)
    assert is_stale(projection, authority) is False
    # The lease recorded when the run was attached binds the projection.
    lease = workspace_leases(store, "ws-1")[0]
    assert lease.lease_id == projection.lease_id
    assert lease.owner_execution == "run-a"
    # Lifecycle behaviour is untouched: an attached workspace stays IN_USE.
    assert prepared.state is WorkspaceState.IN_USE


def test_snapshot_records_mutation_set_of_projected_execution(
    store: WorkspaceStore, local_root: Path
):
    create_workspace(
        store, workspace_id="ws-1", owner_scope="task", root=str(local_root), kind="local"
    )
    attach_workspace(store, "ws-1", "run-a")
    prepare_workspace(store, "ws-1", task_id="run-a")
    (local_root / "a.py").write_text("x = 1\n", encoding="utf-8")

    snapshot = snapshot_workspace(store, "ws-1", task_id="run-a")

    projection = workspace_projection(store, "ws-1")
    mutation_set = store.get("ws-1").metadata["mutation_set"]
    assert mutation_set["projection_id"] == projection.projection_id
    assert mutation_set["base_revision"] == projection.base_revision.to_dict()
    assert mutation_set["execution_id"] == "run-a"
    # Lifecycle state is untouched by the read-only snapshot.
    assert snapshot.generation == 0
    assert store.get("ws-1").state is WorkspaceState.IN_USE


def test_release_drops_the_recorded_lease(store: WorkspaceStore, local_root: Path):
    create_workspace(
        store, workspace_id="ws-1", owner_scope="task", root=str(local_root), kind="local"
    )
    attach_workspace(store, "ws-1", "run-a")
    assert len(workspace_leases(store, "ws-1")) == 1

    release_workspace(store, "ws-1", "run-a")

    assert workspace_leases(store, "ws-1") == ()
    assert store.get("ws-1").active_run_ids == ()


# -- P1-07: stale projection surfaces as a conflict ----------------------------


def test_recovery_advances_revision_and_flags_stale_projection(
    store: WorkspaceStore, local_root: Path
):
    create_workspace(
        store, workspace_id="ws-1", owner_scope="task", root=str(local_root), kind="local"
    )
    prepare_workspace(store, "ws-1")
    authority_before = workspace_authority(store, "ws-1")
    projection_before = workspace_projection(store, "ws-1")
    assert is_stale(projection_before, authority_before) is False

    ready = recover_workspace(store, "ws-1", is_live=DEAD_RUN)

    authority_after = workspace_authority(store, "ws-1")
    projection_after = workspace_projection(store, "ws-1")
    assert ready.state is WorkspaceState.READY
    assert ready.generation == 1
    assert (
        authority_after.current_revision.revision_id
        != authority_before.current_revision.revision_id
    )
    assert authority_after.current_revision.monotonic == 1
    # The projection was NOT re-pointed at the newer revision.
    assert projection_after.base_revision == authority_before.current_revision
    assert projection_after.state is ProjectionState.CONFLICT
    state, reason = reconcile_projection(projection_after, authority_after)
    assert state is ProjectionState.CONFLICT
    assert "conflict" in reason


def test_stale_projection_records_conflict_and_never_silently_succeeds(
    store: WorkspaceStore, local_root: Path
):
    create_workspace(
        store, workspace_id="ws-1", owner_scope="task", root=str(local_root), kind="local"
    )
    attach_workspace(store, "ws-1", "run-a")
    prepare_workspace(store, "ws-1")
    base_revision = workspace_projection(store, "ws-1").base_revision

    recover_workspace(store, "ws-1", is_live=DEAD_RUN)

    conflict = workspace_projection_conflict(store, "ws-1")
    authority = workspace_authority(store, "ws-1")
    assert conflict["conflict"] == ConflictClass.REVISION_CONFLICT.value
    assert conflict["strategy"] == ConflictStrategy.MERGE.value
    assert conflict["state"] in {ProjectionState.STALE.value, ProjectionState.CONFLICT.value}
    assert conflict["base_revision_id"] == base_revision.revision_id
    assert conflict["authority_revision_id"] == authority.current_revision.revision_id

    # A later prepare must not silently repair the diverged projection.
    prepare_workspace(store, "ws-1")
    still = workspace_projection(store, "ws-1")
    assert still.base_revision == base_revision
    assert still.state is ProjectionState.CONFLICT
    assert workspace_projection_conflict(store, "ws-1") == conflict
    assert is_stale(still, workspace_authority(store, "ws-1")) is True


def test_reset_is_the_explicit_baseline_that_clears_a_diverged_projection(
    store: WorkspaceStore, local_root: Path
):
    create_workspace(
        store, workspace_id="ws-1", owner_scope="task", root=str(local_root), kind="local"
    )
    prepare_workspace(store, "ws-1")
    recover_workspace(store, "ws-1", is_live=DEAD_RUN)
    assert workspace_projection_conflict(store, "ws-1") is not None

    reset = reset_workspace(store, "ws-1", is_live=DEAD_RUN)

    assert reset.generation == 2
    assert workspace_projection_conflict(store, "ws-1") is None
    prepare_workspace(store, "ws-1")
    promoted = workspace_projection(store, "ws-1")
    assert promoted.state is ProjectionState.ACTIVE
    assert is_stale(promoted, workspace_authority(store, "ws-1")) is False


def test_conflict_classification_maps_to_documented_strategies():
    assert classify_conflict() is None
    assert classify_conflict(path_overlap=True) is ConflictClass.PATH_CONFLICT
    assert classify_conflict(base_mismatch=True) is ConflictClass.REVISION_CONFLICT
    assert classify_conflict(git_diverged=True) is ConflictClass.GIT_DIVERGENCE
    assert classify_conflict(lease_held=True) is ConflictClass.LEASE_CONFLICT
    assert classify_conflict(authority_changed=True) is ConflictClass.AUTHORITY_CHANGED

    documented = {
        ConflictClass.PATH_CONFLICT: ConflictStrategy.REPROJECT,
        ConflictClass.REVISION_CONFLICT: ConflictStrategy.MERGE,
        ConflictClass.GIT_DIVERGENCE: ConflictStrategy.REBASE,
        ConflictClass.LEASE_CONFLICT: ConflictStrategy.RETRY,
        ConflictClass.AUTHORITY_CHANGED: ConflictStrategy.HUMAN_REVIEW,
    }
    for conflict, strategy in documented.items():
        assert resolve_conflict(conflict) is strategy

    # Precedence is fail-closed: authority identity outranks every other signal.
    assert (
        classify_conflict(authority_changed=True, lease_held=True, base_mismatch=True)
        is ConflictClass.AUTHORITY_CHANGED
    )


def test_reconcile_never_repairs_a_diverged_projection():
    revision = new_revision("ws-1", monotonic=0)
    authority = WorkspaceAuthority(
        workspace_id="ws-1",
        canonical_root="/tmp/ws",
        authority_type=AuthorityType.FILESYSTEM,
        current_revision=revision,
    )
    projection = new_projection("ws-1", revision, path="/tmp/ws")
    moved = replace(
        authority,
        current_revision=replace(revision, revision_id="revision-after", monotonic=1),
    )

    assert reconcile_projection(projection, authority) == (ProjectionState.READY, "MATCH")
    assert is_stale(projection, moved) is True
    state, reason = reconcile_projection(projection, moved)
    assert state is ProjectionState.STALE
    assert reason.startswith("DIVERGED")
    conflicted = reconcile_projection(replace(projection, state=ProjectionState.CONFLICT), moved)
    assert conflicted == (ProjectionState.CONFLICT, "already in conflict")


def test_workspace_kind_maps_to_authority_type():
    handle = WorkspaceHandle(
        workspace_id="ws-1", owner_scope="task", root="/wt", kind="git_worktree"
    )
    assert _authority_type(handle) is AuthorityType.GIT
    assert _authority_type(replace(handle, kind="local")) is AuthorityType.FILESYSTEM
    assert _authority_type(replace(handle, kind="sandbox")) is AuthorityType.HYBRID


class _FakeWorktreeManager:
    """Reports the git record the lifecycle verifies against."""

    def __init__(self, *, branch: str = "task-x", changed_files: tuple[str, ...] = ()):
        from runtime.coding.worktree import WorktreeRecord

        self.record = WorktreeRecord(
            task_id="t",
            branch_name=branch,
            path="/wt",
            repo_root="/repo",
            clean=not changed_files,
            changed_files=list(changed_files),
        )

    def status(self, task_id=None, *, path=None):
        return self.record


def _git_workspace(store: WorkspaceStore, *, branch: str = "task-x") -> WorkspaceHandle:
    return create_workspace(
        store,
        workspace_id="ws-git",
        owner_scope="task",
        root="/wt",
        kind="git_worktree",
        metadata={"repo_root": "/repo", "task_id": "t", "branch": branch},
    )


def test_prepare_surfaces_git_divergence_with_the_rebase_strategy(store: WorkspaceStore):
    _git_workspace(store, branch="task-x")
    prepare_workspace(store, "ws-git", worktree_manager=_FakeWorktreeManager(branch="task-x"))
    promoted = workspace_projection(store, "ws-git")
    assert promoted.backend == "git_worktree"
    assert workspace_projection_conflict(store, "ws-git") is None

    # The verified head moved off the branch recorded at creation.
    prepare_workspace(store, "ws-git", worktree_manager=_FakeWorktreeManager(branch="task-y"))

    conflict = workspace_projection_conflict(store, "ws-git")
    assert conflict["conflict"] == ConflictClass.GIT_DIVERGENCE.value
    assert conflict["strategy"] == ConflictStrategy.REBASE.value
    assert conflict["state"] == ProjectionState.CONFLICT.value
    assert "git head moved" in conflict["reason"]
    # No silent repair: the projection is frozen, not re-pointed.
    assert workspace_projection(store, "ws-git").projection_id == promoted.projection_id
    assert workspace_projection(store, "ws-git").base_revision == promoted.base_revision


def test_prepare_surfaces_path_overlap_with_the_reproject_strategy(store: WorkspaceStore):
    _git_workspace(store)
    manager = _FakeWorktreeManager(changed_files=("a.py",))
    prepare_workspace(store, "ws-git", worktree_manager=manager)
    snapshot_workspace(store, "ws-git", worktree_manager=manager)

    prepare_workspace(store, "ws-git", worktree_manager=manager)

    conflict = workspace_projection_conflict(store, "ws-git")
    assert conflict["conflict"] == ConflictClass.PATH_CONFLICT.value
    assert conflict["strategy"] == ConflictStrategy.REPROJECT.value
    assert "changed paths overlap" in conflict["reason"]


# -- P1-08: action receipts on high-impact side effects ------------------------


async def _ledger(tmp_path: Path, goal_run_id: str = "run-receipt") -> tuple[SideEffectLedger, str]:
    repository = DurableExecutionRepository(sqlite_path=tmp_path / "receipts.sqlite3")
    await repository.connect()
    await repository.create_goal_run(goal_run_id=goal_run_id, idempotency_key=goal_run_id)
    item = await repository.enqueue_work_item(
        WorkItemSpec(goal_run_id=goal_run_id, logical_key="publish", kind="tool")
    )
    return SideEffectLedger(repository), item["id"]


async def test_high_impact_side_effect_emits_a_verifiable_receipt(tmp_path: Path):
    ledger, work_item_id = await _ledger(tmp_path)
    calls: list[str] = []

    async def provider():
        calls.append("ran")
        return "published"

    result = await ledger.execute(
        goal_run_id="run-receipt",
        work_item_id=work_item_id,
        operation_key=build_operation_key("run-receipt", work_item_id, "publish"),
        operation_type="publish",
        target_ref="provider:item",
        request={"value": 1},
        provider=provider,
        capability="manual_only",
    )

    assert result == "published"
    receipts = ledger.receipts("run-receipt")
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.goal_run_id == "run-receipt"
    assert receipt.execution_id == work_item_id
    assert receipt.action == "publish"
    assert receipt.capability == "manual_only"
    assert receipt.target == "provider:item"
    assert receipt.request_digest
    assert receipt.previous_receipt_digest is None
    valid, reason = verify_chain(receipts)
    assert valid, reason
    assert ledger.verify_receipts("run-receipt") == (True, "chain valid")

    # Replaying the same operation key stays idempotent: no second receipt, no
    # second provider call.
    replay = await ledger.execute(
        goal_run_id="run-receipt",
        work_item_id=work_item_id,
        operation_key=build_operation_key("run-receipt", work_item_id, "publish"),
        operation_type="publish",
        target_ref="provider:item",
        request={"value": 1},
        provider=provider,
        capability="manual_only",
    )
    assert replay == "published"
    assert calls == ["ran"]
    assert len(ledger.receipts("run-receipt")) == 1
    assert ledger.verify_receipts("run-receipt")[0] is True


async def test_receipts_chain_across_a_goal_run(tmp_path: Path):
    ledger, work_item_id = await _ledger(tmp_path)

    async def provider():
        return "ok"

    for index in range(3):
        await ledger.execute(
            goal_run_id="run-receipt",
            work_item_id=work_item_id,
            operation_key=build_operation_key("run-receipt", work_item_id, f"op-{index}"),
            operation_type=f"op-{index}",
            target_ref=f"provider:item-{index}",
            request={"value": index},
            provider=provider,
            capability="manual_only",
        )

    receipts = ledger.receipts("run-receipt")
    assert len(receipts) == 3
    assert [item.action for item in receipts] == ["op-0", "op-1", "op-2"]
    for previous, current in itertools.pairwise(receipts):
        assert current.previous_receipt_digest == previous.signature
    assert verify_chain(receipts) == (True, "chain valid")


def test_verify_chain_rejects_a_tampered_receipt():
    from server.action_receipt import new_receipt

    first = new_receipt(
        goal_run_id="g",
        execution_id="e",
        agent_id="bot",
        action="op-0",
        capability="manual_only",
        target="provider:item-0",
    )
    second = new_receipt(
        goal_run_id="g",
        execution_id="e",
        agent_id="bot",
        action="op-1",
        capability="manual_only",
        target="provider:item-1",
        previous_receipt_digest=first.signature,
    )
    tampered = replace(second, target="provider:someone-else")
    assert isinstance(tampered, ActionReceipt)
    valid, reason = verify_chain([first, tampered])
    assert valid is False
    assert "signature mismatch" in reason

    orphan = new_receipt(
        goal_run_id="g",
        execution_id="e",
        agent_id="bot",
        action="op-2",
        capability="manual_only",
        target="provider:item-2",
        previous_receipt_digest=None,
    )
    valid, reason = verify_chain([first, orphan])
    assert valid is False
    assert "chain broken" in reason


async def test_declared_class_and_capability_drive_receipt_coverage(tmp_path: Path):
    ledger, work_item_id = await _ledger(tmp_path)

    async def provider():
        return "ok"

    # capability "none" has no replay guarantee at all -> EXTERNAL_MUTATION.
    await ledger.execute(
        goal_run_id="run-receipt",
        work_item_id=work_item_id,
        operation_key=build_operation_key("run-receipt", work_item_id, "raw"),
        operation_type="raw",
        target_ref="provider:raw",
        request={},
        provider=provider,
        capability="none",
    )
    # An explicit low-impact class is honoured (no receipt).
    await ledger.execute(
        goal_run_id="run-receipt",
        work_item_id=work_item_id,
        operation_key=build_operation_key("run-receipt", work_item_id, "read"),
        operation_type="read",
        target_ref="provider:read",
        request={},
        provider=provider,
        capability="idempotency_key",
        side_effect_class="NONE",
    )

    receipts = ledger.receipts("run-receipt")
    assert [item.action for item in receipts] == ["raw"]
    assert verify_chain(receipts)[0] is True


async def test_receipt_failure_never_breaks_the_side_effect(tmp_path: Path, monkeypatch):
    import server.action_receipt as receipt_module

    ledger, work_item_id = await _ledger(tmp_path)

    def boom(**_kwargs):
        raise RuntimeError("receipt plane down")

    monkeypatch.setattr(receipt_module, "new_receipt", boom)
    calls: list[str] = []

    async def provider():
        calls.append("ran")
        return "published"

    result = await ledger.execute(
        goal_run_id="run-receipt",
        work_item_id=work_item_id,
        operation_key=build_operation_key("run-receipt", work_item_id, "publish"),
        operation_type="publish",
        target_ref="provider:item",
        request={"value": 1},
        provider=provider,
        capability="manual_only",
    )

    assert result == "published"
    assert calls == ["ran"]
    assert ledger.receipts("run-receipt") == []
    assert ledger.receipt_failures == 1
