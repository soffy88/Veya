"""D4 gates: generic workspace lifecycle (single authority).

Matrix: create/attach/prepare/snapshot/recover/reset/release/destroy,
active-lease denial, path-escape denial, orphan recovery, bounded GC,
session-independence (D3 x D4), restart survival, and real wiring through
CodingTaskService create/resume/cancel. No skips, no swallowed exceptions.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runtime.coding.workspace_lifecycle import (
    WorkspaceBusyError,
    WorkspaceConflictError,
    WorkspaceError,
    WorkspaceHandle,
    WorkspaceKind,
    WorkspacePathError,
    WorkspaceState,
    WorkspaceStore,
    WorkspaceUnsupportedError,
    attach_workspace,
    collect_garbage,
    create_workspace,
    destroy_workspace,
    prepare_workspace,
    recover_workspace,
    release_workspace,
    reset_workspace,
    snapshot_workspace,
)


@pytest.fixture()
def store(tmp_path: Path) -> WorkspaceStore:
    return WorkspaceStore(tmp_path / "veya")


@pytest.fixture()
def events() -> list[tuple[str, dict]]:
    return []


def _emit(events: list[tuple[str, dict]]):
    def _record(topic: str, payload: dict) -> None:
        events.append((topic, payload))

    return _record


def _local(store: WorkspaceStore, root: Path, ws_id: str = "ws-1") -> WorkspaceHandle:
    root.mkdir(parents=True, exist_ok=True)
    return create_workspace(
        store, workspace_id=ws_id, owner_scope="task", root=str(root), kind="local"
    )


class _FakeManager:
    """Stand-in for WorktreeManager: records delegation, never reimplements."""

    def __init__(self, *, branch: str = "task-x", clean: bool = True, fail: bool = False):
        from runtime.coding.worktree import WorktreeRecord

        self.calls: list[str] = []
        self._record = WorktreeRecord(
            task_id="x",
            branch_name=branch,
            path="/wt",
            repo_root="/repo",
            clean=clean,
            changed_files=[] if clean else ["a.py"],
        )
        self._fail = fail
        self.discarded: list[tuple[str, bool]] = []

    def status(self, task_id=None, *, path=None):
        self.calls.append("status")
        if self._fail:
            from runtime.coding.worktree import WorktreeError

            raise WorktreeError("gone")
        return self._record

    def discard(self, task_id: str, *, force: bool = False):
        self.discarded.append((task_id, force))
        return self._record


# -- model / store -----------------------------------------------------------


def test_kind_state_coercion_fail_closed():
    assert WorkspaceKind.coerce("GIT_WORKTREE") is WorkspaceKind.GIT_WORKTREE
    with pytest.raises(ValueError):
        WorkspaceKind.coerce("zfs")
    handle = WorkspaceHandle(workspace_id="w", owner_scope="t", root="/r", kind="local")
    assert handle.state is WorkspaceState.NEW
    assert handle.active_run_ids == ()
    with pytest.raises(ValueError):
        WorkspaceHandle(workspace_id=" ", owner_scope="t", root="/r")
    with pytest.raises(ValueError):
        WorkspaceHandle.from_dict(
            {"workspace_id": "w", "owner_scope": "t", "root": "/r", "state": "nope"}
        )


def test_store_roundtrip_and_restart(tmp_path: Path):
    first = WorkspaceStore(tmp_path / "v")
    handle = _local(first, tmp_path / "r")
    reopened = WorkspaceStore(tmp_path / "v")
    assert reopened.get("ws-1") == handle
    assert [item.workspace_id for item in reopened.list()] == ["ws-1"]
    assert reopened.get("missing") is None
    with pytest.raises(ValueError):
        first.get("../escape")
    corrupt = tmp_path / "v" / "workspaces" / "bad.json"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{nope", encoding="utf-8")
    assert first.get("bad") is None


# -- create / attach ----------------------------------------------------------


def test_create_idempotent_and_conflict(store: WorkspaceStore, tmp_path: Path):
    root = tmp_path / "r"
    first = _local(store, root)
    again = create_workspace(
        store, workspace_id="ws-1", owner_scope="task", root=str(root), kind="local"
    )
    assert again == first
    assert len(store.list()) == 1
    with pytest.raises(WorkspaceConflictError):
        create_workspace(
            store,
            workspace_id="ws-1",
            owner_scope="task",
            root=str(tmp_path / "other"),
            kind="local",
        )


def test_attach_transitions_and_dup_idempotent(store: WorkspaceStore, tmp_path: Path, events):
    _local(store, tmp_path / "r")
    bound = attach_workspace(store, "ws-1", "run-a", emit=_emit(events))
    assert bound.state is WorkspaceState.IN_USE
    assert bound.active_run_ids == ("run-a",)
    assert events[0][0] == "workspace.attached"
    same = attach_workspace(store, "ws-1", "run-a")
    assert same == bound
    assert len(store.list()) == 1
    with pytest.raises(WorkspaceError):
        attach_workspace(store, "ghost", "run-a")


def test_attach_refuses_broken_and_destroyed(store: WorkspaceStore, tmp_path: Path):
    create_workspace(
        store,
        workspace_id="ws-1",
        owner_scope="task",
        root=str(tmp_path / "gone"),
        kind="local",
    )
    with pytest.raises(WorkspaceError):
        recover_workspace(store, "ws-1")
    assert store.get("ws-1").state is WorkspaceState.BROKEN
    with pytest.raises(WorkspaceError, match="cannot attach broken"):
        attach_workspace(store, "ws-1", "run-a")
    tomb = destroy_workspace(store, "ws-1", managed_root=tmp_path)
    assert tomb.state is WorkspaceState.DESTROYED
    with pytest.raises(WorkspaceError, match="cannot attach destroyed"):
        attach_workspace(store, "ws-1", "run-a")


# -- prepare / snapshot --------------------------------------------------------


def test_prepare_local_and_git_paths(store: WorkspaceStore, tmp_path: Path):
    _local(store, tmp_path / "r")
    ready = prepare_workspace(store, "ws-1")
    assert ready.state is WorkspaceState.READY
    assert ready.last_used_at >= ready.created_at

    manager = _FakeManager()
    git = create_workspace(
        store,
        workspace_id="ws-git",
        owner_scope="task",
        root="/wt",
        kind="git_worktree",
        metadata={"repo_root": "/repo", "task_id": "x"},
    )
    assert git.state is WorkspaceState.NEW
    ready_git = prepare_workspace(store, "ws-git", worktree_manager=manager)
    assert ready_git.state is WorkspaceState.READY
    assert manager.calls == ["status"]

    create_workspace(
        store,
        workspace_id="ws-remote",
        owner_scope="task",
        root="ssh://h/x",
        kind="remote",
    )
    with pytest.raises(WorkspaceUnsupportedError):
        prepare_workspace(store, "ws-remote")


def test_snapshot_records_refs_only(store: WorkspaceStore, tmp_path: Path, events):
    _local(store, tmp_path / "r")
    snap = snapshot_workspace(store, "ws-1", artifact_refs=["art-1"], emit=_emit(events))
    assert snap.workspace_id == "ws-1"
    assert snap.generation == 0
    assert snap.artifact_refs == ("art-1",)
    assert snap.branch is None
    assert events[0][0] == "workspace.snapshot"
    # Read-only: lifecycle state untouched.
    assert store.get("ws-1").state is WorkspaceState.NEW

    manager = _FakeManager(branch="task-x", clean=False)
    create_workspace(
        store,
        workspace_id="ws-git",
        owner_scope="task",
        root="/wt",
        kind="git_worktree",
        metadata={"repo_root": "/repo", "task_id": "x"},
    )
    git_snap = snapshot_workspace(store, "ws-git", worktree_manager=manager)
    assert git_snap.branch == "task-x"
    assert git_snap.clean is False
    assert git_snap.dirty_hash is not None


# -- recover / reset -----------------------------------------------------------


def test_recover_orphan_ready_and_generation(store: WorkspaceStore, tmp_path: Path):
    _local(store, tmp_path / "r")
    attach_workspace(store, "ws-1", "run-dead")
    ready = recover_workspace(store, "ws-1", is_live=lambda run: False)
    assert ready.state is WorkspaceState.READY
    assert ready.generation == 1


def test_recover_missing_record_never_recreates(store: WorkspaceStore):
    with pytest.raises(WorkspaceError, match="refusing silent recreate"):
        recover_workspace(store, "ghost")


def test_recover_missing_resource_goes_broken(store: WorkspaceStore, tmp_path: Path):
    missing = tmp_path / "gone"
    create_workspace(
        store, workspace_id="ws-1", owner_scope="task", root=str(missing), kind="local"
    )
    with pytest.raises(WorkspaceError):
        recover_workspace(store, "ws-1")
    assert store.get("ws-1").state is WorkspaceState.BROKEN


def test_recover_live_refuses(store: WorkspaceStore, tmp_path: Path):
    _local(store, tmp_path / "r")
    attach_workspace(store, "ws-1", "run-live")
    with pytest.raises(WorkspaceBusyError):
        recover_workspace(store, "ws-1", is_live=lambda run: True)


def test_reset_guard_and_baseline(store: WorkspaceStore, tmp_path: Path):
    _local(store, tmp_path / "r")
    attach_workspace(store, "ws-1", "run-live")
    with pytest.raises(WorkspaceBusyError):
        reset_workspace(store, "ws-1", is_live=lambda run: True)
    with pytest.raises(WorkspaceBusyError):
        reset_workspace(store, "ws-1")
    calm = reset_workspace(store, "ws-1", is_live=lambda run: False)
    assert calm.state is WorkspaceState.READY
    assert calm.active_run_ids == ()
    assert calm.generation == 1


# -- release --------------------------------------------------------------------


def test_release_policies_and_double_release(store: WorkspaceStore, tmp_path: Path):
    _local(store, tmp_path / "r")
    attach_workspace(store, "ws-1", "run-a")
    attach_workspace(store, "ws-1", "run-b")
    partial = release_workspace(store, "ws-1", "run-a")
    assert partial.state is WorkspaceState.IN_USE
    assert partial.active_run_ids == ("run-b",)
    released = release_workspace(store, "ws-1", "run-b")
    assert released.state is WorkspaceState.RELEASED
    assert release_workspace(store, "ws-1", "run-b") == released
    assert release_workspace(store, "ghost", "run-x") is None


def test_release_without_retain_returns_ready(store: WorkspaceStore, tmp_path: Path):
    _local(store, tmp_path / "r")
    attach_workspace(store, "ws-1", "run-a")
    ready = release_workspace(store, "ws-1", "run-a", retain=False)
    assert ready.state is WorkspaceState.READY


# -- destroy / GC -----------------------------------------------------------------


def test_destroy_denies_active_and_escape(store: WorkspaceStore, tmp_path: Path):
    _local(store, tmp_path / "r")
    attach_workspace(store, "ws-1", "run-live")
    with pytest.raises(WorkspaceBusyError):
        destroy_workspace(store, "ws-1", managed_root=tmp_path)
    release_workspace(store, "ws-1", "run-live")
    with pytest.raises(WorkspacePathError):
        destroy_workspace(store, "ws-1", managed_root=tmp_path / "elsewhere")
    with pytest.raises(WorkspacePathError):
        destroy_workspace(store, "ws-1", managed_root=tmp_path / "r")


def test_destroy_git_worktree_delegates_and_tombstones(store: WorkspaceStore, tmp_path: Path):
    root = tmp_path / "wt"
    root.mkdir()
    manager = _FakeManager()
    create_workspace(
        store,
        workspace_id="ws-git",
        owner_scope="task",
        root=str(root),
        kind="git_worktree",
        metadata={"repo_root": str(tmp_path), "task_id": "task-9"},
    )
    tomb = destroy_workspace(
        store, "ws-git", managed_root=tmp_path, worktree_manager=manager, force=True
    )
    assert tomb.state is WorkspaceState.DESTROYED
    assert manager.discarded == [("task-9", True)]
    assert root.is_dir()  # the fake stood in; delegation itself is asserted above
    again = destroy_workspace(store, "ws-git", managed_root=tmp_path)
    assert again.state is WorkspaceState.DESTROYED
    assert manager.discarded == [("task-9", True)]


def test_destroy_local_never_deletes_filesystem(store: WorkspaceStore, tmp_path: Path):
    root = tmp_path / "repo"
    (root / "code.py").parent.mkdir(parents=True, exist_ok=True)
    (root / "code.py").write_text("x = 1\n", encoding="utf-8")
    _local(store, root)
    tomb = destroy_workspace(store, "ws-1", managed_root=tmp_path)
    assert tomb.state is WorkspaceState.DESTROYED
    assert (root / "code.py").is_file()


def test_destroy_repo_root_refused(store: WorkspaceStore, tmp_path: Path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    _local(store, root)
    with pytest.raises(WorkspacePathError):
        destroy_workspace(store, "ws-1", managed_root=tmp_path)


def test_collect_garbage_only_released_disposable(store: WorkspaceStore, tmp_path: Path):
    keep = tmp_path / "keep-wt"
    keep.mkdir()
    gone = tmp_path / "gone-wt"
    gone.mkdir()
    manager = _FakeManager()
    for ws_id, path in (("ws-keep", keep), ("ws-gone", gone)):
        create_workspace(
            store,
            workspace_id=ws_id,
            owner_scope="task",
            root=str(path),
            kind="git_worktree",
            metadata={"repo_root": str(tmp_path), "task_id": ws_id},
        )
        attach_workspace(store, ws_id, "run")
        release_workspace(store, ws_id, "run")
    attach_workspace(store, "ws-keep", "run-live")
    report = collect_garbage(store, managed_root=tmp_path, worktree_manager_factory=lambda: manager)
    assert report["destroyed"] == ["ws-gone"]
    assert store.get("ws-keep").state is WorkspaceState.IN_USE
    assert store.get("ws-gone").state is WorkspaceState.DESTROYED


def test_collect_garbage_skips_broken_and_local(store: WorkspaceStore, tmp_path: Path):
    create_workspace(
        store,
        workspace_id="ws-broken",
        owner_scope="task",
        root=str(tmp_path / "missing"),
        kind="local",
    )
    with pytest.raises(WorkspaceError):
        recover_workspace(store, "ws-broken")
    _local(store, tmp_path / "repo", ws_id="ws-local")
    attach_workspace(store, "ws-local", "run-old")
    release_workspace(store, "ws-local", "run-old")
    report = collect_garbage(store, managed_root=tmp_path)
    assert report["destroyed"] == []
    reasons = {item["workspace_id"]: item["reason"] for item in report["skipped"]}
    assert reasons["ws-broken"] == "broken-needs-human"
    assert reasons["ws-local"] == "kind-not-disposable"


# -- session independence ----------------------------------------------------------


def test_fresh_session_may_reuse_workspace_files():
    from runtime.execution.resume import decide_resume_disposition

    session_decision = decide_resume_disposition(trigger="user_rerun", session_id="sess-old")
    assert session_decision.disposition.value == "fresh_user_retry"
    # The file decision is separate: an existing READY workspace is reused,
    # never deleted because the session went fresh.
    assert session_decision.session_id == "sess-old"


# -- real git worktree integration ------------------------------------------------------


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True
    )
    (path / "main.py").write_text("def hello():\n    return 'hello'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return path


def test_real_worktree_prepare_snapshot_destroy(tmp_path: Path):
    from runtime.coding.worktree import WorktreeManager

    repo = _git_repo(tmp_path / "repo")
    store = WorkspaceStore(tmp_path / "veya")
    manager = WorktreeManager(repo)
    record = manager.create("task-e2e", "objective")
    handle = create_workspace(
        store,
        workspace_id="ws-e2e",
        owner_scope="task",
        root=record.path,
        kind="git_worktree",
        metadata={"task_id": "task-e2e", "repo_root": str(repo)},
    )
    assert handle.state is WorkspaceState.NEW
    ready = prepare_workspace(store, "ws-e2e")
    assert ready.state is WorkspaceState.READY
    snap = snapshot_workspace(store, "ws-e2e")
    assert snap.branch
    assert snap.clean is True
    assert snap.dirty_hash is not None
    tomb = destroy_workspace(store, "ws-e2e", managed_root=repo / ".veya" / "worktrees")
    assert tomb.state is WorkspaceState.DESTROYED
    assert not Path(record.path).exists()


# -- wiring: coding task service -----------------------------------------------------------


def _service(tmp_path: Path):
    from runtime.coding.task_service import CodingTaskService

    return CodingTaskService(tmp_path)


@pytest.mark.asyncio()
async def test_wiring_create_attaches_and_prepares(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    service = _service(repo)
    from runtime.coding.task_service import CodingTaskRequest

    request = CodingTaskRequest(workspace_path=str(repo), objective="wired", source="cli")
    state = await service.create_task(request)
    assert state.lifecycle_workspace_id
    stored = WorkspaceStore(repo / ".veya").get(state.lifecycle_workspace_id)
    assert stored is not None
    assert stored.state is WorkspaceState.IN_USE
    assert stored.active_run_ids == (state.task_id,)
    assert stored.root == state.worktree_path


@pytest.mark.asyncio()
async def test_wiring_resume_recovers_and_cancel_releases(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    service = _service(repo)
    from runtime.coding.task_service import CodingTaskRequest

    state = await service.create_task(
        CodingTaskRequest(workspace_path=str(repo), objective="wired", source="cli")
    )
    task_id = state.task_id
    ws_id = state.lifecycle_workspace_id
    resumed = await service.resume_task(task_id)
    assert resumed.resume_decision is not None
    assert resumed.resume_decision["disposition"] == "recover_checkpoint"
    assert WorkspaceStore(repo / ".veya").get(ws_id).state is WorkspaceState.IN_USE
    await service.cancel_task(task_id)
    released = WorkspaceStore(repo / ".veya").get(ws_id)
    assert released.state is WorkspaceState.RELEASED
    assert released.active_run_ids == ()


@pytest.mark.asyncio()
async def test_wiring_broken_workspace_fails_closed(tmp_path: Path):
    import shutil

    repo = _git_repo(tmp_path / "repo")
    service = _service(repo)
    from runtime.coding.task_service import CodingTaskRequest, WorktreeError

    state = await service.create_task(
        CodingTaskRequest(workspace_path=str(repo), objective="wired", source="cli")
    )
    shutil.rmtree(state.worktree_path, ignore_errors=True)
    subprocess.run(["git", "worktree", "prune"], cwd=repo, check=True, capture_output=True)
    with pytest.raises(WorktreeError):
        await service.resume_task(state.task_id)
    assert (
        WorkspaceStore(repo / ".veya").get(state.lifecycle_workspace_id).state
        is WorkspaceState.BROKEN
    )


@pytest.mark.asyncio()
async def test_wiring_fresh_rerun_reuses_workspace(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    service = _service(repo)
    from runtime.coding.task_service import CodingTaskRequest, CodingTaskResult

    state = await service.create_task(
        CodingTaskRequest(workspace_path=str(repo), objective="wired", source="cli")
    )
    from runtime.coding.task_service import _write_task_state

    st = service.get_task_state(state.task_id)
    st.status = "failed"
    _write_task_state(repo, st)

    async def fake_run(task_id, leaves, *, goal_run_id=None):
        assert goal_run_id is None
        return CodingTaskResult(task_id, None, "completed", None, [], [], "fake", True)

    service.run_task = fake_run  # type: ignore[method-assign]
    result = await service.resume_task(
        state.task_id, [{"tool": "x", "args": {}}], trigger="user_rerun"
    )
    assert result.resume_decision["disposition"] == "fresh_user_retry"
    workspace = WorkspaceStore(repo / ".veya").get(state.lifecycle_workspace_id)
    assert workspace.state is WorkspaceState.IN_USE
    assert Path(state.worktree_path).is_dir()
