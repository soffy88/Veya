"""Q4: provider-independent positive execution closure for L2.

These tests pin the selection semantics (capability + health), the provider
failover rules, and the single-authority property of the deterministic L0 mode.
They never assert on executor prose: every claim is checked against the durable
GoalRun state and the real filesystem.
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path
from typing import Any

import pytest

from veya.remote.executor_health import (
    ExecutorHealth,
    ExecutorHealthRegistry,
    ProviderFailureClass,
    WorkerCapabilities,
)
from veya.supervision import runner as mission_runner
from veya.supervision.models import Mission, MissionPolicies
from veya.supervision.runner import (
    _LOCAL_EXECUTOR,
    _active_executors,
    _replay_safe,
    canonical_runner,
    executor_candidates,
    executor_inventory,
    select_mission_executor,
)

WRITE_ACTION = {
    "tool": "write_file",
    "arguments": {"filepath": ".veya/qualification/q4/probe.txt", "content": "VEYA_Q4_TOKEN\n"},
}


def _repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "existing.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


def _mission(
    workspace: Path, *, executor: str | None = "opencode", actions: list[dict] | None = None
) -> Mission:
    execution_policy: dict[str, Any] = {}
    if executor is not None:
        execution_policy["assignee_hint"] = executor
    if actions:
        execution_policy["actions"] = actions
    mission = Mission(mission_id="m-q4", goal="create probe", workspace=str(workspace))
    mission.policies = MissionPolicies(execution_policy=execution_policy)
    return mission


def _healthy(*executors: str) -> ExecutorHealthRegistry:
    health = ExecutorHealthRegistry()
    for name in executors:
        health.record_success(name)
    return health


# ── selection: capability ────────────────────────────────────────────────


def test_executor_selection_uses_capability() -> None:
    """A read-only requirement must not select a file-mutating-only executor."""
    health = _healthy("opencode", "dsh")
    read_only = WorkerCapabilities(supports_read_task=True)
    candidates = executor_candidates(
        required_capabilities=read_only.to_dict(), health_registry=health
    )
    dsh = next(c for c in candidates if c.executor_id == "dsh")
    assert dsh.capability_satisfied is True
    assert dsh.eligible is False  # dsh is not authenticated

    mutation = WorkerCapabilities(supports_file_effect=True).to_dict()
    dsh_mutation = next(
        c
        for c in executor_candidates(required_capabilities=mutation, health_registry=health)
        if c.executor_id == "dsh"
    )
    assert dsh_mutation.capability_satisfied is False
    assert dsh_mutation.eligible is False


def test_inventory_reports_every_admitted_executor_with_real_evidence() -> None:
    inventory = {row["executor_id"]: row for row in executor_inventory()}
    # The inventory must be exactly what the canonical registry declares, so
    # supervision cannot hold a second, drifting executor list.
    assert set(inventory) == set(_active_executors())
    for row in inventory.values():
        assert set(row) >= {
            "executor_id",
            "qualified",
            "reachable",
            # `authenticated` is gone: it was computed as credential_present and
            # reported a file's existence as authentication. Validity is tri-state
            # and carries the reason it could not be established.
            "credential_valid",
            "credential_evidence",
            "capability_satisfied",
            "health",
            "admission_supported",
            "provider_dependency",
        }
        # eligibility is a projection, not a stored claim
        assert isinstance(row["eligible"], bool)
    # builtin is the in-process substrate: no provider dependency
    assert inventory[_LOCAL_EXECUTOR]["provider_dependency"] is False


def test_builtin_requires_resolved_actions_to_be_a_target() -> None:
    without = executor_candidates(local_capable=False)
    builtin_without = next(c for c in without if c.executor_id == _LOCAL_EXECUTOR)
    assert builtin_without.eligible is False
    with_actions = executor_candidates(local_capable=True)
    builtin_with = next(c for c in with_actions if c.executor_id == _LOCAL_EXECUTOR)
    assert builtin_with.eligible is True


# ── selection: health ────────────────────────────────────────────────────


def test_executor_selection_uses_health() -> None:
    health = _healthy("opencode", "dsh")
    health.record_failure("opencode", ProviderFailureClass.PROVIDER_UNAVAILABLE)
    assert health.get_health("opencode") == ExecutorHealth.UNAVAILABLE

    selected, eligible = select_mission_executor(
        requested="opencode",
        required_capabilities=WorkerCapabilities(supports_file_effect=True).to_dict(),
        health_registry=health,
    )
    assert selected is None or selected.executor_id != "opencode"
    assert all(c.executor_id != "opencode" for c in eligible if c.eligible)


def test_unhealthy_executor_not_selected_when_alternative_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead provider must not be selected while a healthy capable one exists."""

    def fake_candidates(**kwargs: Any) -> list[Any]:
        common = dict(
            local=False,
            capability_satisfied=True,
            reachable=True,
            credential_valid=True,
            admission_supported=True,
            provider_dependency=True,
        )
        return [
            mission_runner.ExecutorCandidate(
                executor_id="opencode", health="UNAVAILABLE", **common
            ),
            mission_runner.ExecutorCandidate(executor_id="dsh", health="HEALTHY", **common),
        ]

    monkeypatch.setattr(mission_runner, "executor_candidates", fake_candidates)
    selected, eligible = select_mission_executor(requested="opencode")
    assert selected is not None
    assert selected.executor_id == "dsh"
    assert "dsh" in [c.executor_id for c in eligible]


def test_unhealthy_hicode_not_required_when_alternative_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Q4 mission must not need hicode once it is known unhealthy."""
    health = _healthy("opencode", "dsh")
    health.record_failure("opencode", ProviderFailureClass.PROVIDER_UNAVAILABLE)
    selected, _ = select_mission_executor(
        requested="opencode",
        required_capabilities=WorkerCapabilities(supports_file_effect=True).to_dict(),
        health_registry=health,
    )
    assert selected is None or selected.executor_id != "opencode"


# ── failover ─────────────────────────────────────────────────────────────


async def test_provider_failure_can_retask(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_run(mission: Any, *, assignee: str, actions: list[dict]) -> Any:
        calls.append(assignee)
        if assignee == "opencode":
            return types.SimpleNamespace(
                goal_id="g1",
                status="blocked",
                tasks={},
                final_summary="cliproxy-google: status 400 bad gateway",
                unfinished_work=["provider failure"],
                block_reason="provider unavailable",
                execution_delta={
                    "execution_created": [],
                    "execution_modified": [],
                    "execution_deleted": [],
                },
                attributed_path_count=0,
                budget={},
            )
        return types.SimpleNamespace(
            goal_id="g2",
            status="completed",
            tasks={},
            final_summary="done",
            unfinished_work=[],
        )

    def fake_candidates(**kwargs: Any) -> list[Any]:
        common = dict(
            local=False,
            capability_satisfied=True,
            reachable=True,
            credential_valid=True,
            admission_supported=True,
            provider_dependency=True,
        )
        return [
            mission_runner.ExecutorCandidate(executor_id="opencode", health="HEALTHY", **common),
            mission_runner.ExecutorCandidate(executor_id="dsh", health="HEALTHY", **common),
        ]

    monkeypatch.setattr(mission_runner, "_run_canonical_goal", fake_run)
    monkeypatch.setattr(mission_runner, "executor_candidates", fake_candidates)

    mission = _mission(Path("/tmp"), executor="opencode", actions=[WRITE_ACTION])
    result = await canonical_runner(mission)
    assert calls == ["opencode", "dsh"]
    assert result.status == "completed"
    assert result.executor_substitution["failed_executor"] == "opencode"
    assert result.executor_substitution["selected_executor"] == "dsh"


def test_no_failover_when_side_effect_replay_is_unsafe() -> None:
    """A provider failure after a real side effect must not replay the work."""
    dirty = types.SimpleNamespace(
        goal_id="g1",
        status="blocked",
        final_summary="provider unavailable",
        execution_delta={
            "execution_created": ["probe.txt"],
            "execution_modified": [],
            "execution_deleted": [],
        },
        attributed_path_count=1,
        budget={},
    )
    assert _replay_safe(dirty) is False

    clean = types.SimpleNamespace(
        goal_id="g1",
        status="blocked",
        final_summary="provider unavailable",
        execution_delta={
            "execution_created": [],
            "execution_modified": [],
            "execution_deleted": [],
        },
        attributed_path_count=0,
        budget={},
    )
    assert _replay_safe(clean) is True

    committed_action = types.SimpleNamespace(
        goal_id="g1",
        status="blocked",
        final_summary="provider unavailable",
        execution_delta={
            "execution_created": [],
            "execution_modified": [],
            "execution_deleted": [],
        },
        attributed_path_count=0,
        budget={"last_canonical_action": {"result": {"executed": True}}},
    )
    assert _replay_safe(committed_action) is False


async def test_no_available_executor_blocks_truthfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No eligible executor must block with a typed reason, never fake success."""

    def fake_candidates(**kwargs: Any) -> list[Any]:
        return [
            mission_runner.ExecutorCandidate(
                executor_id="opencode",
                local=False,
                capability_satisfied=True,
                reachable=True,
                credential_valid=True,
                health="UNAVAILABLE",
                admission_supported=True,
                provider_dependency=True,
            )
        ]

    monkeypatch.setattr(mission_runner, "executor_candidates", fake_candidates)
    mission = _mission(Path("/tmp"), executor="opencode")
    result = await canonical_runner(mission)
    assert result.status == "blocked"
    assert result.block_reason == "NO_HEALTHY_EXECUTION_TARGET"
    assert result.unfinished_work
    assert result.executor_inventory


async def test_provider_failure_never_reports_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(mission: Any, *, assignee: str, actions: list[dict]) -> Any:
        return types.SimpleNamespace(
            goal_id="g1",
            status="blocked",
            tasks={},
            final_summary="hicode provider 402 insufficient balance",
            unfinished_work=["provider"],
            block_reason="blocked",
            execution_delta={
                "execution_created": [],
                "execution_modified": [],
                "execution_deleted": [],
            },
            attributed_path_count=0,
            budget={},
        )

    def fake_candidates(**kwargs: Any) -> list[Any]:
        return [
            mission_runner.ExecutorCandidate(
                executor_id="opencode",
                local=False,
                capability_satisfied=True,
                reachable=True,
                credential_valid=True,
                health="HEALTHY",
                admission_supported=True,
                provider_dependency=True,
            )
        ]

    monkeypatch.setattr(mission_runner, "_run_canonical_goal", fake_run)
    monkeypatch.setattr(mission_runner, "executor_candidates", fake_candidates)
    result = await canonical_runner(_mission(Path("/tmp"), executor="opencode"))
    assert result.status == "blocked"
    assert result.status != "completed"


# ── single execution authority ───────────────────────────────────────────


def test_goalrun_uses_canonical_action_gateway() -> None:
    """The deterministic mode must cross GoalRun's one physical boundary."""
    import inspect

    from server.goal_run.canonical_worker import CanonicalWorkerAdapter

    body = inspect.getsource(CanonicalWorkerAdapter.execute_resolved_actions)
    # the only physical crossing available to the resolved-action mode
    assert "execute_canonical_action" in body
    # it must not open its own tool, policy, or permission path
    assert "master_tools.execute" not in body
    assert "PermissionEngine(" not in body
    assert "ActionGatewayAdapter(" not in body


def test_deterministic_action_does_not_require_second_execution_authority() -> None:
    """One adapter, one gateway binding, one side-effect ledger."""
    import inspect

    from server.goal_run.canonical_worker import CanonicalWorkerAdapter

    adapter = CanonicalWorkerAdapter(task_id="t", objective="o")
    assert adapter.action_gateway is None
    assert adapter.resolved_actions == []
    # the resolved-action mode reuses the adapter's own bound gateway/executor
    source = inspect.getsource(CanonicalWorkerAdapter)
    assert source.count("self.action_gateway = ActionGatewayAdapter(") == 1
    assert source.count("SideEffectLedger(") == 1
    # and it never constructs a second one
    assert (
        inspect.getsource(CanonicalWorkerAdapter.execute_resolved_actions).count(
            "SideEffectLedger("
        )
        == 0
    )


async def test_resolved_action_writes_real_file_through_permission_engine(
    tmp_path: Path,
) -> None:
    """End-to-end: resolved action -> ActionGateway -> PermissionEngine -> file."""
    root = _repo(tmp_path / "proj")
    from server.goal_run.canonical_worker import CanonicalWorkerAdapter
    from server.goal_run.models import GoalRunState

    target = ".veya/qualification/q4-positive/probe.txt"
    token = "VEYA_Q4_LIVE_TOKEN_abc123"
    state = GoalRunState(goal_id="goal-q4-test", goal_text="create probe")
    adapter = CanonicalWorkerAdapter(task_id="task-q4", objective=state.goal_text)
    await adapter.before_execution(state, str(root))
    adapter.computer_id = "computer-q4"
    adapter.resolved_actions = [
        {"tool": "write_file", "arguments": {"filepath": target, "content": token + "\n"}},
        {"tool": "read_hashline", "arguments": {"filepath": target}},
    ]
    adapter.gateway_executor = mission_runner._local_action_dispatch(str(root))

    class _Task:
        id = "task-q4"
        instruction = "create probe"

    leaf = await adapter.execute_resolved_actions(state, _Task())
    assert leaf is not None
    assert leaf.status == "completed", leaf.block_reason

    written = root / target
    assert written.is_file()
    assert written.read_text(encoding="utf-8").strip() == token
    # the write is recorded through the ActionGateway's side-effect ledger
    assert adapter.action_gateway is not None
    assert adapter.execution_repository is not None


async def test_resolved_action_fails_closed_on_unknown_tool(tmp_path: Path) -> None:
    root = _repo(tmp_path / "proj2")
    from server.goal_run.canonical_worker import CanonicalWorkerAdapter
    from server.goal_run.models import GoalRunState

    state = GoalRunState(goal_id="goal-q4-bad", goal_text="bad")
    adapter = CanonicalWorkerAdapter(task_id="task-q4-bad", objective=state.goal_text)
    await adapter.before_execution(state, str(root))
    adapter.computer_id = "computer-q4-bad"
    adapter.resolved_actions = [{"tool": "definitely_not_a_tool", "arguments": {}}]
    adapter.gateway_executor = mission_runner._local_action_dispatch(str(root))

    class _Task:
        id = "task-q4-bad"
        instruction = "bad"

    leaf = await adapter.execute_resolved_actions(state, _Task())
    assert leaf.status == "blocked"
    assert not list(root.glob(".veya/qualification/q4-positive/probe.txt"))


def test_mission_create_rejects_unregistered_resolved_action(tmp_path: Path) -> None:
    from server.supervision_tools import veya_mission_create

    with pytest.raises(ValueError, match="unknown canonical tool"):
        veya_mission_create(
            str(tmp_path),
            "g",
            executor="builtin",
            actions=[{"tool": "not_a_real_tool", "arguments": {}}],
        )


def test_mission_create_persists_resolved_actions(tmp_path: Path) -> None:
    from server.supervision_tools import veya_mission_create

    created = veya_mission_create(
        str(tmp_path),
        "g",
        executor="builtin",
        actions=[{"tool": "write_file", "arguments": {"filepath": "a.txt", "content": "x"}}],
    )
    stored = created["mission"]["policies"]["execution_policy"]["actions"]
    assert stored == [{"tool": "write_file", "arguments": {"filepath": "a.txt", "content": "x"}}]


# ── execution delta / HEAD transition ────────────────────────────────────


def test_head_transition_does_not_create_false_deletions(tmp_path: Path) -> None:
    """A tracked file committed mid-run must not be reported as deleted."""
    from server.goal_run.execution_delta import capture_git_state, execution_delta

    root = _repo(tmp_path / "proj3")
    (root / "existing.py").write_text("x = 2\n", encoding="utf-8")
    before = capture_git_state(str(root))
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "executor commit"], check=True)
    after = capture_git_state(str(root))
    delta = execution_delta(before, after)
    assert delta["execution_deleted"] == []
    assert delta["preexisting_committed"] == ["existing.py"]
    assert delta["before_head"] != delta["after_head"]


def test_execution_delta_survives_executor_commit(tmp_path: Path) -> None:
    """A HEAD transition plus a new artifact keeps both facts distinct."""
    from server.goal_run.execution_delta import capture_git_state, execution_delta

    root = _repo(tmp_path / "proj4")
    (root / "existing.py").write_text("x = 3\n", encoding="utf-8")
    before = capture_git_state(str(root))
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "mid-run"], check=True)
    (root / ".veya").mkdir(parents=True, exist_ok=True)
    (root / ".veya" / "probe.txt").write_text("token\n", encoding="utf-8")
    after = capture_git_state(str(root))
    delta = execution_delta(before, after)
    assert delta["execution_deleted"] == []
    assert delta["execution_created"] == [".veya/probe.txt"]
    assert delta["preexisting_committed"] == ["existing.py"]


def test_runner_does_not_mutate_permission_engine() -> None:
    """Selection must read the permission engine, never redefine it."""
    import inspect

    from veya.supervision import runner as runner_module

    source = inspect.getsource(runner_module)
    assert "PermissionEngine(" not in source
    assert "class PermissionEngine" not in source
    # the only permission authority the mission plane may reference is the
    # engine's decision vocabulary, never a second evaluator
    assert "from veya.remote.permission_engine import" not in source
