"""DSH execution plane: dedicated config, gateway binding, executor hint wiring."""

from __future__ import annotations

import types

from server import dsh_plane
from veya.supervision import runner as runner_mod
from veya.supervision.models import Mission


def test_config_file_then_process_env_wins(tmp_path, monkeypatch):
    cfg = tmp_path / "dsh.env"
    cfg.write_text(
        "# comment\nDSH_RUNTIME=ENABLED\nDSH_MODEL=model-from-file\nDSH_SESSION_DIR=/tmp/ignored\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VEYA_DSH_CONFIG", str(cfg))
    assert dsh_plane.model() == "model-from-file"
    monkeypatch.setenv("DSH_MODEL", "model-from-env")
    assert dsh_plane.model() == "model-from-env"
    # /tmp must never be the real state dir unless explicitly configured to be.
    monkeypatch.setenv("DSH_SESSION_DIR", str(tmp_path / "state"))
    assert dsh_plane.state_dir() == tmp_path / "state"


def test_runtime_gate(tmp_path, monkeypatch):
    cfg = tmp_path / "dsh.env"
    cfg.write_text("DSH_RUNTIME=DISABLED\n", encoding="utf-8")
    monkeypatch.setenv("VEYA_DSH_CONFIG", str(cfg))
    assert dsh_plane.is_enabled() is False
    cfg.write_text("DSH_RUNTIME=ENABLED\n", encoding="utf-8")
    assert dsh_plane.is_enabled() is True


def test_plane_env_points_at_veya_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_DSH_CONFIG", str(tmp_path / "missing.env"))
    monkeypatch.setenv("DSH_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DSH_MODEL", "some-gateway-model")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = dsh_plane.load_config()
    env = dsh_plane.subprocess_env(cfg)
    assert env["DEEPSEEK_BASE_URL"] == dsh_plane.DEFAULT_BASE_URL
    assert env["DEEPSEEK_DEFAULT_MODEL"] == "some-gateway-model"
    # DSH never holds the real provider credential.
    assert env["DEEPSEEK_API_KEY"] == dsh_plane.DEFAULT_API_KEY
    assert env["DSH_HOME"].startswith(str(tmp_path / "state"))
    assert "127.0.0.1" in env["NO_PROXY"]


def test_model_patch_binds_default_model(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_DSH_CONFIG", str(tmp_path / "missing.env"))
    monkeypatch.setenv("DSH_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DSH_MODEL", "gateway/model-x")
    cfg = dsh_plane.load_config()
    path = dsh_plane.model_patch_path(cfg)
    text = path.read_text(encoding="utf-8")
    assert "id: gateway/model-x" in text
    assert "model: gateway/model-x" in text
    argv = dsh_plane.dsh_argv("/usr/bin/dsh", "TASK", cfg)
    assert argv[:3] == ["/usr/bin/dsh", "--profile", "headless"]
    assert argv[3:5] == ["--patch", str(path)]
    assert argv[-1] == "TASK"


def test_unknown_executor_hint_is_ignored():
    mission = Mission(mission_id="m1", goal="g")
    assert runner_mod._assignee_hint(mission) is None
    mission.policies.execution_policy["assignee_hint"] = "nonsense"
    assert runner_mod._assignee_hint(mission) is None
    mission.policies.execution_policy["assignee_hint"] = "dsh"
    assert runner_mod._assignee_hint(mission) == "dsh"


def test_canonical_runner_routes_through_goalrun_authority(monkeypatch):
    """L2 execution is owned by GoalRun, not the retired ``project_ask`` leg.

    The mission still declares the executor it was admitted with, but that hint
    is a *preference* resolved by capability and health, and the chosen
    executor travels to GoalRun as a task ``assignee``.  ``canonical_runner``
    must not re-enter the old project dispatch entry.
    """
    seen: dict[str, object] = {}

    class _State:
        goal_id = "goal-dsh"
        status = "completed"
        final_summary = "done"

        def __init__(self) -> None:
            self.tasks: dict[str, object] = {}
            self.unfinished_work: list[str] = []

    async def fake_project_run_goal(**kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace(goal_id="goal-dsh", status="completed", summary="done")

    import server.goal_run.runner as goalrun_runner
    import server.goal_run.store as goalrun_store

    monkeypatch.setattr(goalrun_runner, "project_run_goal", fake_project_run_goal)
    monkeypatch.setattr(goalrun_store, "load_goal_run", lambda *_a, **_k: _State())

    # The retired leg must not be used for execution.
    def _forbidden(*_a, **_k):
        raise AssertionError("canonical_runner must not dispatch through project_ask")

    import server.project_ask as project_ask

    monkeypatch.setattr(project_ask, "project_ask", _forbidden)

    import asyncio

    mission = Mission(mission_id="m2", goal="g", workspace="/tmp/ws")
    # An unauthenticated/unavailable hint is a preference, not a pin: selection
    # must move to an eligible executor rather than execute on the named one.
    mission.policies.execution_policy["assignee_hint"] = "dsh"
    mission.policies.execution_policy["actions"] = [
        {"tool": "write_file", "arguments": {"filepath": "probe.txt", "content": "x"}}
    ]

    out = asyncio.run(runner_mod.canonical_runner(mission))

    # L2 -> GoalRun is the one execution authority.
    assert seen, "canonical_runner must execute through project_run_goal"
    assert seen["project_root"] == "/tmp/ws"
    tasks = seen["tasks"]
    assert isinstance(tasks, list) and len(tasks) == 1
    # the selected executor is carried by the canonical execution task
    assert tasks[0]["assignee"] in {"builtin", "hicode"}
    assert tasks[0]["instruction"] == "g"
    assert out.final_summary == "done"


def test_canonical_runner_blocks_when_no_executor_is_eligible(monkeypatch):
    """No eligible executor must fail closed instead of reaching for GoalRun."""
    seen: dict[str, object] = {}

    async def _forbidden(**kwargs):  # pragma: no cover - must not be reached
        seen.update(kwargs)
        raise AssertionError("must not execute without an eligible executor")

    import server.goal_run.runner as goalrun_runner

    monkeypatch.setattr(goalrun_runner, "project_run_goal", _forbidden)

    import asyncio

    mission = Mission(mission_id="m3", goal="g", workspace="/tmp/ws")
    mission.policies.execution_policy["assignee_hint"] = "dsh"

    out = asyncio.run(runner_mod.canonical_runner(mission))

    assert not seen
    assert out.status == "blocked"
    assert out.block_reason == "NO_HEALTHY_EXECUTION_TARGET"
    assert out.unfinished_work
